"""Check prices and decide when to alert.

Alert rules:
- With a target price: alert when the price is at/below target. Re-arms once the price goes back above target.
- Without a target: alert when the price is below the previous check. Re-arms once the price rises above the
  last alerted price. The first check just records a baseline.
- Either way, never alert twice for the same or a higher price (`last_notified_price`).

Politeness (to avoid looking like a bot and getting blocked):
- Requests to the same store within one run are spaced out by a random gap (Pacer).
- When a store blocks us (HTTP 403/429), that product is skipped for 1, then 2, 4, ... checks (capped), and the
  first successful check resets it.

Shopify stores: automatic discounts ("15% Off Sitewide") only show up in the cart, so when the fetcher can price a
cart (CartPricer), the cart's price is used: as is without a selector, or if lower than the selector's price.
"""

import logging
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from price_tracker import db
from price_tracker.extract import extract_name, extract_price
from price_tracker.models import CartPrice, HTTPStatusError, PriceDrop, Product, format_price

log = logging.getLogger(__name__)

# Random pause, in seconds, between two requests to the same store in one run.
SAME_STORE_GAP_SECONDS = (3.0, 8.0)
# Statuses that mean "you look like a bot" rather than a broken page.
BLOCK_STATUSES = frozenset({403, 429})
# Longest back-off, in checks (4 days at the default 12-hour interval).
MAX_SKIPS = 8


class Fetcher(Protocol):
    def __call__(self, url: str, *, render: bool = False, wait_for: str | None = None) -> str: ...


@runtime_checkable
class CartPricer(Protocol):
    """A fetcher that can also price a product in the store's cart (see shopify.py)."""

    def cart_price(self, url: str, html: str) -> CartPrice | None: ...


Notifier = Callable[[PriceDrop], None]


@dataclass(frozen=True)
class CheckResult:
    product: Product
    price: Decimal | None = None
    drop: PriceDrop | None = None
    error: str | None = None
    # True when this check switched the product to headless-browser fetching (see _fetch_price).
    switched_to_render: bool = False
    # True when the product wasn't fetched at all because it's backing off after a block.
    skipped: bool = False


class Pacer:
    """Spaces out requests to the same store within one check run: back-to-back hits on a site look automated.
    Different stores aren't delayed. `clock`/`sleep` are injectable for tests."""

    def __init__(
        self,
        gap_seconds: tuple[float, float] | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._gap = gap_seconds or SAME_STORE_GAP_SECONDS
        self._rng = rng or random.Random()
        self._clock = clock
        self._sleep = sleep
        self._last_done: dict[str, float] = {}

    def wait(self, store: str) -> None:
        """Call before requesting from `store`; sleeps if the last request to it finished too recently."""
        last = self._last_done.get(store)
        if last is not None:
            delay = last + self._rng.uniform(*self._gap) - self._clock()
            if delay > 0:
                self._sleep(delay)

    def done(self, store: str) -> None:
        self._last_done[store] = self._clock()


def evaluate(product: Product, price: Decimal) -> tuple[PriceDrop | None, Decimal | None]:
    """Return (alert or None, the new last_notified_price)."""
    notified = product.last_notified_price
    target = product.target_price

    if target is not None:
        if price > target:
            return None, None  # back above target: re-arm
        if notified is not None and price >= notified:
            return None, notified
        return PriceDrop(
            product, product.last_price, price, f"At or below your target of {format_price(target)}"
        ), price

    if notified is not None and price > notified:
        notified = None  # rebounded above the last alert: re-arm
    previous = product.last_price
    if previous is None or price >= previous or (notified is not None and price >= notified):
        return None, notified
    percent = (previous - price) / previous * 100
    return PriceDrop(product, previous, price, f"Dropped {percent:.1f}% from {format_price(previous)}"), price


def _fetch_price(product: Product, fetch: Fetcher) -> tuple[str, Decimal | None, bool]:
    """Return (html, price, rendered). Plain HTTP first; if that errors or has no price, retry once in headless
    Chromium, which gets past JavaScript bot checks and sees prices drawn by scripts. Errors from the last attempt
    propagate."""
    if product.render:
        html = fetch(product.url, render=True, wait_for=product.selector)
        return html, extract_price(html, product.selector), True
    try:
        html = fetch(product.url, render=False, wait_for=product.selector)
        price = extract_price(html, product.selector)
        if price is not None:
            return html, price, False
        problem = "no price"
    except Exception as e:
        problem = f"error: {e}"
    log.info("Plain fetch of %s gave %s; retrying in a headless browser", product.label, problem)
    html = fetch(product.url, render=True, wait_for=product.selector)
    return html, extract_price(html, product.selector), True


def _with_cart_price(
    product: Product, fetch: Fetcher, html: str, page_price: Decimal
) -> tuple[Decimal, tuple[str, ...]]:
    """Return (price, discount titles), preferring what the cart charges. Never fails the check: any problem
    pricing the cart just keeps the page price."""
    if not isinstance(fetch, CartPricer):
        return page_price, ()
    try:
        cart = fetch.cart_price(product.url, html)
    except Exception as e:
        log.info("Couldn't price %s in the cart: %s", product.label, e)
        return page_price, ()
    if cart is None:
        return page_price, ()
    # A selector is explicit, and may point at a sale a script applies outside Shopify's discounts (so the cart
    # charges more); take whichever is lower. Without one, the cart is authoritative for the variant.
    if product.selector is not None and page_price <= cart.price:
        return page_price, ()
    if cart.price != page_price:
        log.info("%s: cart price %s vs page %s %s", product.label, cart.price, page_price, list(cart.discounts))
    return cart.price, cart.discounts


def check_product(conn: sqlite3.Connection, product: Product, fetch: Fetcher, notify: Notifier) -> CheckResult:
    try:
        html, price, rendered = _fetch_price(product, fetch)
    except HTTPStatusError as e:
        if e.status not in BLOCK_STATUSES:
            log.error("Fetch failed for %s: %s", product.label, e)
            return CheckResult(product, error=f"fetch failed: {e}")
        # Back off: keep hitting a store that's blocking us and a temporary block can become a lasting one.
        streak = product.blocked_streak + 1
        skips = min(2 ** (streak - 1), MAX_SKIPS)
        db.record_block(conn, product.id, streak, skips)
        log.warning(
            "%s blocked us (HTTP %d); skipping %s for %d check(s)", product.store, e.status, product.label, skips
        )
        return CheckResult(product, error=f"blocked by {product.store} (HTTP {e.status}); pausing for {skips} check(s)")
    except Exception as e:  # one bad site shouldn't stop the rest
        log.error("Fetch failed for %s: %s", product.label, e)
        return CheckResult(product, error=f"fetch failed: {e}")

    if price is None:
        # The page's own title/name usually says what we got instead (a bot check, "Access Denied", a 404 page...).
        log.error("No price found for %s; page was %r (%d bytes)", product.label, extract_name(html), len(html))
        return CheckResult(product, error="no price found, even in a headless browser (try a selector)")

    price, discounts = _with_cart_price(product, fetch, html, price)

    switched = rendered and not product.render
    if switched:
        # Remember per product, so later checks skip the plain fetch that's known not to work. Saved before
        # notifying: it's a fact about the site, independent of whether this check's alert goes out.
        log.info("%s needs a headless browser; using one from now on", product.label)
        db.enable_render(conn, product.id)

    drop, new_notified = evaluate(product, price)
    if drop is not None and discounts:
        drop = replace(drop, reason=f"{drop.reason} ({', '.join(discounts)})")
    if drop is not None:
        try:
            notify(drop)
        except Exception as e:
            # Skip recording so the next check compares against the same baseline and retries the alert.
            log.error("Notification failed for %s: %s", product.label, e)
            return CheckResult(product, price=price, error=f"notification failed: {e}", switched_to_render=switched)

    # Notify-then-record: if recording fails after a successful send, the alert may repeat (at-least-once).
    # Only take the name from a page that yielded a price: a bot-check or error page has a title too.
    db.record_price(conn, product, price, new_notified, page_name=extract_name(html))
    return CheckResult(product, price=price, drop=drop, switched_to_render=switched)


def check_all(
    conn: sqlite3.Connection,
    fetch: Fetcher,
    notify: Notifier,
    channel_id: int | None = None,
    pacer: Pacer | None = None,
    checked_before: datetime | None = None,
) -> list[CheckResult]:
    """Check every product, or only one channel's when `channel_id` is given. Least recently checked go first.
    With `checked_before`, skip products last checked at or after then (their price is still fresh)."""
    pacer = pacer or Pacer()
    results = []
    for product in db.products_to_check(conn, channel_id, checked_before):
        if product.skips_left > 0:
            db.use_skip(conn, product.id)
            results.append(CheckResult(product, skipped=True))
            continue
        pacer.wait(product.store)
        try:
            results.append(check_product(conn, product, fetch, notify))
        finally:
            pacer.done(product.store)
    return results
