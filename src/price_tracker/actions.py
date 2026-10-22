"""Chat-facing operations: each returns the reply text. Sync and Discord-agnostic so they're easy to test.

Every operation is scoped to `channel_id`, the channel the command came from: a channel only sees, checks, and
removes its own products, and a product's alerts go back to the channel that tracked it.
"""

import re
import sqlite3
from decimal import ROUND_HALF_UP, Decimal

from price_tracker import db
from price_tracker.extract import parse_price
from price_tracker.models import format_price
from price_tracker.table import product_table
from price_tracker.tracker import CheckResult, Fetcher, Notifier, check_all, check_product

# Discord rejects messages over 2000 characters.
MAX_MESSAGE_LENGTH = 2000

_NOTHING_TRACKED = "Nothing tracked in this channel yet. Use `/track <url>`."
_LIST_FOOTER = "-# `/history <id>` for a product's link and price history"
# "-20%", "+15 %", "12.5%"
_PERCENT = re.compile(r"([+-]?)\s*(\d+(?:\.\d+)?)\s*%")


def track(
    conn: sqlite3.Connection,
    fetch: Fetcher,
    notify: Notifier,
    channel_id: int,
    url: str,
    name: str | None = None,
    target: Decimal | None = None,
    selector: str | None = None,
    render: bool = False,
) -> str:
    if not url.startswith(("http://", "https://")):
        return "URL must start with http:// or https://"
    if any(ch.isspace() for ch in url):
        # Usually other options typed into the url box ("… render:True selector:…"), which Discord sends as text.
        return (
            "URL can't contain spaces. Put only the link in `url`, and pick `render`, `selector`, etc. "
            "as separate options (click them above the message box, or press Tab after the URL)."
        )
    try:
        product = db.add_product(conn, channel_id, url, name, target, selector, render)
    except sqlite3.IntegrityError:
        return f"Already tracking <{url}> in this channel"

    # Check right away so a bad URL/selector surfaces now, and the first price becomes the baseline.
    result = check_product(conn, product, fetch, notify)
    # Re-read: the check may have filled in the page's product name.
    product = db.get_product(conn, product.id) or product
    header = f"Tracking #{product.number}: {product.label}"
    if result.price is None:
        return (
            f"{header}\n⚠️ Couldn't read a price yet ({result.error}). "
            f"It stays tracked; `/untrack {product.number}` to drop it."
        )
    reply = f"{header}\nCurrent price: **{format_price(result.price)}**"
    if result.switched_to_render:
        reply += (
            "\n-# Needed a headless browser (bot check or JavaScript-drawn price); checks will use one from now on."
        )
    if result.error:  # price read, but the alert failed to send
        return f"{reply}\n⚠️ {result.error}"
    return reply + (" — alert sent!" if result.drop else "")


def list_products(conn: sqlite3.Connection, channel_id: int) -> str:
    products = db.list_products(conn, channel_id)
    if not products:
        return _NOTHING_TRACKED
    # Links can't be clicked inside a code block; /history leads with the product's link instead.
    return _fit(product_table(products), code_block=True, keep=2, footer=_LIST_FOOTER)


def untrack(conn: sqlite3.Connection, channel_id: int, number: int) -> str:
    product = db.find_product(conn, channel_id, number)
    if product is None:
        return _no_product(number)
    db.remove_product(conn, product.id)
    return f"Stopped tracking #{number}: {product.label}"


def history(conn: sqlite3.Connection, channel_id: int, number: int, limit: int = 20) -> str:
    product = db.find_product(conn, channel_id, number)
    if product is None:
        return _no_product(number)
    points = db.price_history(conn, product.id)[-limit:]
    # <url> inside the link suppresses Discord's preview card.
    title = f"{product.store}: [{product.label}](<{product.url}>)"
    if not points:
        return f"No prices recorded yet for {title}"
    return _fit(
        [f"**{title}** (last {len(points)})"]
        + [f"`{t.astimezone():%Y-%m-%d %H:%M}` {format_price(p)}" for p, t in points]
    )


def set_target(conn: sqlite3.Connection, channel_id: int, number: int, raw: str | None) -> str:
    """Set (`150`, `-20%`) or, with no value, clear a product's target price."""
    product = db.find_product(conn, channel_id, number)
    if product is None:
        return _no_product(number)
    if raw is None or not raw.strip():
        db.set_target(conn, product.id, None)
        return f"Cleared the target for #{number} {product.label}; you'll be alerted on any price drop."
    try:
        target, percent = parse_target(raw, product.last_price)
    except ValueError as e:
        return str(e)

    db.set_target(conn, product.id, target)
    reply = f"Target for #{number} {product.label} set to **{format_price(target)}**"
    if percent is not None:
        assert product.last_price is not None  # parse_target needs it for a percentage
        # :f, not str(): a normalized Decimal("20") prints as "2E+1".
        reply += f" ({percent:f}% below the current {format_price(product.last_price)})"
    if product.last_price is not None and product.last_price <= target:
        reply += "\nThe current price is already at or below it, so you'll get an alert at the next check."
    return reply


def parse_target(raw: str, current: Decimal | None) -> tuple[Decimal, Decimal | None]:
    """Parse a target: an amount ("150", "$149.99", "1.299,00 €") or a percentage below `current` ("-20%").
    Returns (target, percent below current or None). Raises ValueError with a message meant for the user."""
    text = raw.strip()
    if match := _PERCENT.fullmatch(text):
        sign, number = match.groups()
        percent = Decimal(number)
        if sign != "-":
            raise ValueError(f"Use a negative percentage for a target below the current price, e.g. `-{number}%`.")
        if not 0 < percent < 100:
            raise ValueError("The percentage must be above 0 and below 100, e.g. `-20%`.")
        if current is None:
            raise ValueError(
                "There's no current price yet to take a percentage of. Use an amount, or try after a check."
            )
        target = (current * (100 - percent) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return target, percent.normalize()
    # parse_price pulls the first number out of the text, so a leading minus would silently become positive.
    price = None if text.startswith("-") else parse_price(text)
    if price is None:
        raise ValueError(f"`{raw}` isn't a price. Use an amount like `150` or a percentage like `-20%`.")
    return price, None


def check_now(conn: sqlite3.Connection, fetch: Fetcher, notify: Notifier, channel_id: int | None) -> str:
    """Check this channel's products; None checks every channel (used by the scheduled check)."""
    results = check_all(conn, fetch, notify, channel_id)
    if not results:
        return _NOTHING_TRACKED
    return _fit([_result_line(r) for r in results], keep=0)


def _no_product(number: int) -> str:
    return f"No product #{number} in this channel"


def _result_line(r: CheckResult) -> str:
    if r.skipped:
        # skips_left is from before this check used one, so it's also how many checks until the retry.
        retry_in = r.product.skips_left
        when = "next check" if retry_in == 1 else f"{retry_in} checks"
        return f"⏸️ #{r.product.number} {r.product.label}: skipped, {r.product.store} blocked us; retrying in {when}"
    if r.error:
        return f"❌ #{r.product.number} {r.product.label}: {r.error}"
    marker = "📉" if r.drop else "✅"
    assert r.price is not None  # set whenever there's no error
    return f"{marker} #{r.product.number} {r.product.label}: {format_price(r.price)}"


def _fit(lines: list[str], *, code_block: bool = False, keep: int = 1, footer: str = "") -> str:
    """Build one Discord message from `lines`, dropping trailing lines (with an "…and N more" note) to stay within
    the length limit. `code_block` wraps the lines in a monospace block (keeps table columns aligned); the first
    `keep` lines (a heading, or a table's header and separator) are never dropped."""

    def render(kept: list[str]) -> str:
        body = "\n".join(kept)
        parts = [f"```\n{body}\n```" if code_block else body]
        if dropped := len(lines) - len(kept):
            parts.append(f"…and {dropped} more")
        if footer:
            parts.append(footer)
        return "\n".join(parts)

    kept = lines
    while len(render(kept)) > MAX_MESSAGE_LENGTH and len(kept) > keep:
        kept = kept[:-1]
    return render(kept)
