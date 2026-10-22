import random
import sqlite3
from decimal import Decimal

import httpx
import pytest

from price_tracker import db, tracker
from price_tracker.models import CartPrice, HTTPStatusError, PriceDrop
from price_tracker.tracker import Pacer, check_all

URL = "https://shop/item"


def _page(price: str) -> str:
    return f'<script type="application/ld+json">{{"@type": "Product", "offers": {{"price": "{price}"}}}}</script>'


class FakeSite:
    def __init__(self) -> None:
        self.price = "100"
        self.fail = False
        self.calls: list[tuple[str, bool, str | None]] = []

    def __call__(self, url: str, *, render: bool = False, wait_for: str | None = None) -> str:
        self.calls.append((url, render, wait_for))
        if self.fail:
            raise httpx.ConnectError("boom")
        return _page(self.price)


class Recorder:
    def __init__(self) -> None:
        self.drops: list[PriceDrop] = []
        self.fail = False

    def __call__(self, drop: PriceDrop) -> None:
        if self.fail:
            raise httpx.HTTPStatusError("429", request=httpx.Request("POST", "x"), response=httpx.Response(429))
        self.drops.append(drop)


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


@pytest.fixture
def notify() -> Recorder:
    return Recorder()


def _run(conn: sqlite3.Connection, site: FakeSite, notify: Recorder, *prices: str) -> list[Decimal]:
    """Check once per price; return the alerted prices."""
    before = len(notify.drops)
    for price in prices:
        site.price = price
        check_all(conn, site, notify)
    return [d.new_price for d in notify.drops[before:]]


def test_first_check_is_baseline(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    db.add_product(conn, 1, URL)
    assert _run(conn, site, notify, "100") == []


def test_alerts_on_drop_with_old_price(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    db.add_product(conn, 1, URL)
    assert _run(conn, site, notify, "100", "80") == [Decimal("80")]
    drop = notify.drops[0]
    assert drop.old_price == Decimal("100")
    assert "20.0%" in drop.reason


def test_no_repeat_alert_while_price_holds(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    db.add_product(conn, 1, URL)
    assert _run(conn, site, notify, "100", "80", "80", "80") == [Decimal("80")]


def test_further_drop_alerts_again(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    db.add_product(conn, 1, URL)
    assert _run(conn, site, notify, "100", "80", "70") == [Decimal("80"), Decimal("70")]


def test_rebound_rearms(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    db.add_product(conn, 1, URL)
    # 80 alert -> 90 (up, re-arm, no alert) -> 85 (drop vs 90) alerts.
    assert _run(conn, site, notify, "100", "80", "90", "85") == [Decimal("80"), Decimal("85")]


def test_increase_never_alerts(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    db.add_product(conn, 1, URL)
    assert _run(conn, site, notify, "100", "110", "120") == []


def test_target_alerts_on_first_check_if_already_below(
    conn: sqlite3.Connection, site: FakeSite, notify: Recorder
) -> None:
    db.add_product(conn, 1, URL, target_price=Decimal("50"))
    assert _run(conn, site, notify, "45") == [Decimal("45")]
    assert notify.drops[0].old_price is None


def test_target_ignores_drops_above_target(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    db.add_product(conn, 1, URL, target_price=Decimal("50"))
    assert _run(conn, site, notify, "100", "80", "60") == []


def test_target_at_exact_price(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    db.add_product(conn, 1, URL, target_price=Decimal("50"))
    assert _run(conn, site, notify, "60", "50.00") == [Decimal("50.00")]


def test_target_rising_below_target_does_not_alert(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    db.add_product(conn, 1, URL, target_price=Decimal("50"))
    # 40 alerts; 45 is still under target but higher, so no alert; 35 is lower, alerts.
    assert _run(conn, site, notify, "40", "45", "35") == [Decimal("40"), Decimal("35")]


def test_target_rearms_after_going_above(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    db.add_product(conn, 1, URL, target_price=Decimal("50"))
    assert _run(conn, site, notify, "40", "60", "48") == [Decimal("40"), Decimal("48")]


def test_fetch_error_is_isolated_and_records_nothing(
    conn: sqlite3.Connection, site: FakeSite, notify: Recorder
) -> None:
    pid = db.add_product(conn, 1, URL).id
    site.fail = True
    [result] = check_all(conn, site, notify)
    assert result.error is not None and "fetch failed" in result.error
    assert db.price_history(conn, pid) == []


def test_one_failing_product_does_not_stop_others(conn: sqlite3.Connection, notify: Recorder) -> None:
    db.add_product(conn, 1, "https://bad")
    good = db.add_product(conn, 1, "https://good").id

    def fetch(url: str, **_: object) -> str:
        if url == "https://bad":
            raise httpx.ConnectError("down")
        return _page("10")

    results = check_all(conn, fetch, notify)
    assert [r.error is None for r in results] == [False, True]
    assert len(db.price_history(conn, good)) == 1


def test_missing_price_is_an_error(conn: sqlite3.Connection, notify: Recorder) -> None:
    pid = db.add_product(conn, 1, URL).id
    [result] = check_all(conn, lambda url, **_: "<html>no price</html>", notify)
    assert result.error is not None and "no price" in result.error
    assert db.price_history(conn, pid) == []


def test_selector_fallback_is_used(conn: sqlite3.Connection, notify: Recorder) -> None:
    pid = db.add_product(conn, 1, URL, selector="#cost").id
    check_all(conn, lambda url, **_: '<b id="cost">$12.34</b>', notify)
    product = db.get_product(conn, pid)
    assert product is not None and product.last_price == Decimal("12.34")


def test_failed_notification_retries_next_check(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    pid = db.add_product(conn, 1, URL).id
    _run(conn, site, notify, "100")

    notify.fail = True
    site.price = "80"
    [result] = check_all(conn, site, notify)
    assert result.error is not None and "notification failed" in result.error
    product = db.get_product(conn, pid)
    assert product is not None and product.last_price == Decimal("100")  # baseline untouched

    notify.fail = False
    assert _run(conn, site, notify, "80") == [Decimal("80")]


def test_passes_render_and_selector_to_fetcher(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    db.add_product(conn, 1, "https://plain")
    db.add_product(conn, 1, "https://js", selector="script", render=True)
    check_all(conn, site, notify)
    assert site.calls == [("https://plain", False, None), ("https://js", True, "script")]


class BotWalledSite:
    """Plain fetches get a challenge page (or an error); only the headless browser sees the product."""

    def __init__(self, plain_raises: bool = False, render_works: bool = True) -> None:
        self.plain_raises = plain_raises
        self.render_works = render_works
        self.calls: list[bool] = []  # render flag per fetch

    def __call__(self, url: str, *, render: bool = False, wait_for: str | None = None) -> str:
        self.calls.append(render)
        if not render:
            if self.plain_raises:
                raise httpx.HTTPStatusError("403", request=httpx.Request("GET", url), response=httpx.Response(403))
            return "<html><title>Just a moment...</title><script>challenge()</script></html>"
        return _page("198.00") if self.render_works else "<html>still blocked</html>"


@pytest.mark.parametrize("plain_raises", [False, True])
def test_falls_back_to_render_and_remembers(conn: sqlite3.Connection, notify: Recorder, plain_raises: bool) -> None:
    pid = db.add_product(conn, 1, URL).id
    site = BotWalledSite(plain_raises=plain_raises)

    [result] = check_all(conn, site, notify)
    assert (result.price, result.error, result.switched_to_render) == (Decimal("198.00"), None, True)
    assert site.calls == [False, True]
    product = db.get_product(conn, pid)
    assert product is not None and product.render is True

    site.calls.clear()
    [result] = check_all(conn, site, notify)
    assert site.calls == [True]  # straight to the browser; no wasted plain fetch
    assert result.switched_to_render is False


def test_no_render_switch_when_browser_fails_too(conn: sqlite3.Connection, notify: Recorder) -> None:
    pid = db.add_product(conn, 1, URL).id
    [result] = check_all(conn, BotWalledSite(render_works=False), notify)
    assert result.error == "no price found, even in a headless browser (try a selector)"
    product = db.get_product(conn, pid)
    assert product is not None and product.render is False


def test_plain_fetch_with_price_never_renders(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    db.add_product(conn, 1, URL)
    check_all(conn, site, notify)
    assert [render for _, render, _ in site.calls] == [False]


def _named_page(price: str, name: str) -> str:
    return (
        f"<html><head><title>{name} | Shop</title>"
        f'<script type="application/ld+json">{{"@type": "Product", "name": "{name}", "offers": {{"price": "{price}"}}}}'
        "</script></head></html>"
    )


def test_fills_name_from_page(conn: sqlite3.Connection, notify: Recorder) -> None:
    pid = db.add_product(conn, 1, URL).id
    check_all(conn, lambda url, **_: _named_page("10", "Deluxe Widget"), notify)
    product = db.get_product(conn, pid)
    assert product is not None and product.label == "Deluxe Widget"


def test_given_name_is_never_overwritten(conn: sqlite3.Connection, notify: Recorder) -> None:
    pid = db.add_product(conn, 1, URL, name="My name").id
    check_all(conn, lambda url, **_: _named_page("10", "Page name"), notify)
    product = db.get_product(conn, pid)
    assert product is not None and product.name == "My name"


def test_name_not_taken_from_page_without_price(conn: sqlite3.Connection, notify: Recorder) -> None:
    pid = db.add_product(conn, 1, URL).id
    check_all(conn, lambda url, **_: "<html><title>Access Denied</title></html>", notify)
    product = db.get_product(conn, pid)
    assert product is not None and product.name is None


def test_alert_reasons_show_two_decimals(conn: sqlite3.Connection, site: FakeSite, notify: Recorder) -> None:
    db.add_product(conn, 1, URL)
    _run(conn, site, notify, "120.0", "99")
    db.add_product(conn, 2, "https://shop/t", target_price=Decimal("50"))
    site.price = "45"
    check_all(conn, site, notify, channel_id=2)
    assert [d.reason for d in notify.drops] == ["Dropped 17.5% from 120.00", "At or below your target of 50.00"]


# --- politeness: same-store pacing and block back-off


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def test_pacer_spaces_requests_to_the_same_store_only() -> None:
    clock = FakeClock()
    pacer = Pacer(gap_seconds=(5.0, 5.0), clock=clock, sleep=clock.sleep)

    for store in ("Acme", "Globex", "Acme"):
        pacer.wait(store)
        clock.now += 1.0  # the request itself
        pacer.done(store)
    # Globex didn't wait; the second Acme request waited 5 s after the first finished, minus the 1 s Globex took.
    assert clock.slept == [4.0]


def test_pacer_gap_is_random_within_range() -> None:
    clock = FakeClock()
    pacer = Pacer(gap_seconds=(3.0, 8.0), rng=random.Random(7), clock=clock, sleep=clock.sleep)
    for _ in range(20):
        pacer.wait("Shop")
        pacer.done("Shop")
    assert all(3.0 <= s <= 8.0 for s in clock.slept)
    assert len(set(clock.slept)) > 10


def test_check_all_paces_each_product_by_store(conn: sqlite3.Connection, notify: Recorder) -> None:
    db.add_product(conn, 1, "https://www.a.com/1")
    db.add_product(conn, 1, "https://b.com/1")
    db.add_product(conn, 1, "https://a.com/2")
    calls: list[str] = []

    class RecordingPacer(Pacer):
        def wait(self, store: str) -> None:
            calls.append(f"wait {store}")

        def done(self, store: str) -> None:
            calls.append(f"done {store}")

    check_all(conn, lambda url, **_: _page("1"), notify, pacer=RecordingPacer())
    assert calls == ["wait A", "done A", "wait B", "done B", "wait A", "done A"]


def test_check_all_paces_by_default(
    conn: sqlite3.Connection, notify: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Production callers don't pass a pacer; check_all must create one rather than skip pacing.
    created: list[Pacer] = []

    class SpyPacer(Pacer):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

    monkeypatch.setattr(tracker, "Pacer", SpyPacer)
    db.add_product(conn, 1, URL)
    check_all(conn, lambda url, **_: _page("1"), notify)
    assert len(created) == 1


class BlockingSite:
    def __init__(self, status: int = 403) -> None:
        self.status = status
        self.blocked = True
        self.fetches = 0

    def __call__(self, url: str, *, render: bool = False, wait_for: str | None = None) -> str:
        self.fetches += 1
        if self.blocked:
            raise HTTPStatusError(self.status, url)
        return _page("10")


def _state(conn: sqlite3.Connection, pid: int) -> tuple[int, int]:
    product = db.get_product(conn, pid)
    assert product is not None
    return product.blocked_streak, product.skips_left


@pytest.mark.parametrize("status", [403, 429])
def test_block_backs_off_exponentially(conn: sqlite3.Connection, notify: Recorder, status: int) -> None:
    pid = db.add_product(conn, 1, URL).id
    site = BlockingSite(status)

    fetched_on = []
    for check in range(1, 31):
        before = site.fetches
        check_all(conn, site, notify)
        if site.fetches > before:
            fetched_on.append(check)
    # Blocked each time it's tried: skip 1, 2, 4, then 8 checks, and stay at 8 (the cap; uncapped would be 16).
    assert fetched_on == [1, 3, 6, 11, 20, 29]
    assert _state(conn, pid)[0] == 6


def test_block_message_and_skip(conn: sqlite3.Connection, notify: Recorder) -> None:
    db.add_product(conn, 1, "https://www.acme.com/s/x", name="Widget")
    [blocked] = check_all(conn, BlockingSite(), notify)
    assert blocked.error == "blocked by Acme (HTTP 403); pausing for 1 check(s)"
    [skipped] = check_all(conn, BlockingSite(), notify)
    assert skipped.skipped and skipped.error is None


def test_success_resets_back_off(conn: sqlite3.Connection, notify: Recorder) -> None:
    pid = db.add_product(conn, 1, URL).id
    site = BlockingSite()
    check_all(conn, site, notify)  # blocked: streak 1, skip 1
    check_all(conn, site, notify)  # skipped
    site.blocked = False
    [result] = check_all(conn, site, notify)
    assert result.price == Decimal("10")
    assert _state(conn, pid) == (0, 0)


@pytest.mark.parametrize("status", [404, 500, 503])
def test_other_http_errors_do_not_back_off(conn: sqlite3.Connection, notify: Recorder, status: int) -> None:
    pid = db.add_product(conn, 1, URL).id
    [result] = check_all(conn, BlockingSite(status), notify)
    assert result.error is not None and result.error.startswith("fetch failed: HTTP")
    assert _state(conn, pid) == (0, 0)


def test_browser_fallback_success_is_not_a_block(conn: sqlite3.Connection, notify: Recorder) -> None:
    pid = db.add_product(conn, 1, URL).id

    def fetch(url: str, *, render: bool = False, wait_for: str | None = None) -> str:
        if not render:
            raise HTTPStatusError(403, url)
        return _page("10")

    [result] = check_all(conn, fetch, notify)
    assert result.price == Decimal("10")
    assert _state(conn, pid) == (0, 0)


# --- Shopify cart pricing


class CartSite:
    """A fetcher that can also price a cart: page price from the HTML, cart price from `cart`."""

    def __init__(self, page_price: str, cart: CartPrice | None | Exception) -> None:
        self.page_price = page_price
        self.cart = cart

    def __call__(self, url: str, *, render: bool = False, wait_for: str | None = None) -> str:
        return _page(self.page_price) + f'<b id="sale">{self.page_price}</b>'

    def cart_price(self, url: str, html: str) -> CartPrice | None:
        if isinstance(self.cart, Exception):
            raise self.cart
        return self.cart


@pytest.mark.parametrize(
    ("page", "cart", "selector", "expected"),
    [
        ("120", CartPrice(Decimal("102"), ("15% Off Sitewide",)), None, Decimal("102")),  # automatic discount
        ("100", CartPrice(Decimal("120"), ()), None, Decimal("120")),  # cart is authoritative for the variant
        ("96", CartPrice(Decimal("160"), ()), "#sale", Decimal("96")),  # script-drawn sale beats the cart
        ("160", CartPrice(Decimal("136"), ("15% Off",)), "#sale", Decimal("136")),  # ...but a lower cart wins
        ("120", None, None, Decimal("120")),  # not Shopify / sold out
        ("120", RuntimeError("cart.js timed out"), None, Decimal("120")),  # a cart problem never fails the check
    ],
)
def test_cart_price_rules(
    conn: sqlite3.Connection,
    notify: Recorder,
    page: str,
    cart: CartPrice | None | Exception,
    selector: str | None,
    expected: Decimal,
) -> None:
    db.add_product(conn, 1, URL, selector=selector)
    [result] = check_all(conn, CartSite(page, cart), notify)
    assert (result.price, result.error) == (expected, None)


def test_alert_names_the_automatic_discount(conn: sqlite3.Connection, notify: Recorder) -> None:
    db.add_product(conn, 1, URL)
    check_all(conn, CartSite("120", CartPrice(Decimal("120"), ())), notify)  # baseline
    check_all(conn, CartSite("120", CartPrice(Decimal("102"), ("15% Off Sitewide",))), notify)
    [drop] = notify.drops
    assert drop.reason == "Dropped 15.0% from 120.00 (15% Off Sitewide)"
