import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from price_tracker import actions, db
from price_tracker.models import PriceDrop
from price_tracker.tracker import Fetcher

A = 111  # channel IDs
B = 222


def _page(price: str) -> str:
    return f'<script type="application/ld+json">{{"@type": "Product", "offers": {{"price": "{price}"}}}}</script>'


def _fetch(price: str = "63.00") -> Fetcher:
    return lambda url, **_: _page(price)


def _no_notify(drop: PriceDrop) -> None:
    raise AssertionError("unexpected notification")


def test_track_records_baseline_price(conn: sqlite3.Connection) -> None:
    reply = actions.track(conn, _fetch(), _no_notify, A, "https://shop/x", name="Widget")
    assert reply == "Tracking #1: Widget\nCurrent price: **63.00**"
    assert [p for p, _ in db.price_history(conn, 1)] == [Decimal("63.00")]


def test_track_assigns_channel(conn: sqlite3.Connection) -> None:
    actions.track(conn, _fetch(), _no_notify, A, "https://shop/x")
    product = db.get_product(conn, 1)
    assert product is not None and product.channel_id == A


def test_track_alerts_immediately_when_already_under_target(conn: sqlite3.Connection) -> None:
    sent: list[PriceDrop] = []
    reply = actions.track(conn, _fetch("40"), sent.append, A, "https://shop/x", target=Decimal("50"))
    assert reply.endswith("— alert sent!")
    assert [(d.new_price, d.product.channel_id) for d in sent] == [(Decimal("40"), A)]


def test_track_reports_alert_failure(conn: sqlite3.Connection) -> None:
    def failing(drop: PriceDrop) -> None:
        raise httpx.ConnectError("discord down")

    reply = actions.track(conn, _fetch("40"), failing, A, "https://shop/x", target=Decimal("50"))
    assert "Current price: **40.00**" in reply
    assert "notification failed" in reply


def test_track_keeps_product_when_price_unreadable(conn: sqlite3.Connection) -> None:
    reply = actions.track(conn, lambda url, **_: "<html></html>", _no_notify, A, "https://shop/x")
    assert "Couldn't read a price yet" in reply and "/untrack 1" in reply
    assert len(db.list_products(conn)) == 1


def test_track_rejects_non_http_url(conn: sqlite3.Connection) -> None:
    assert actions.track(conn, _fetch(), _no_notify, A, "ftp://x").startswith("URL must start with")
    assert db.list_products(conn) == []


def test_track_duplicate_in_same_channel(conn: sqlite3.Connection) -> None:
    actions.track(conn, _fetch(), _no_notify, A, "https://shop/x")
    assert actions.track(conn, _fetch(), _no_notify, A, "https://shop/x") == (
        "Already tracking <https://shop/x> in this channel"
    )


def test_same_url_tracked_independently_per_channel(conn: sqlite3.Connection) -> None:
    actions.track(conn, _fetch(), _no_notify, A, "https://shop/x", target=Decimal("10"))
    reply = actions.track(conn, _fetch(), lambda drop: None, B, "https://shop/x", target=Decimal("70"))
    assert reply.startswith("Tracking #1:")  # numbering is per channel
    assert [p.target_price for p in db.list_products(conn, B)] == [Decimal("70")]


def test_list_is_a_table(conn: sqlite3.Connection) -> None:
    actions.track(conn, _fetch(), _no_notify, A, "https://shop/x", name="Widget", target=Decimal("50"))
    lines = actions.list_products(conn, A).splitlines()
    assert lines[0] == "```" and lines[-2] == "```"
    assert lines[1].split() == ["ID", "Store", "Product", "Now", "Low", "Target", "Checked"]
    assert lines[3].split()[:6] == ["1", "Shop", "Widget", "63.00", "63.00", "50.00"]
    assert lines[-1] == "-# `/history <id>` for a product's link and price history"


def test_list_only_shows_own_channel(conn: sqlite3.Connection) -> None:
    actions.track(conn, _fetch(), _no_notify, A, "https://shop/a", name="InA")
    actions.track(conn, _fetch(), _no_notify, B, "https://shop/b", name="InB")

    listing = actions.list_products(conn, A)
    assert "InA" in listing and "InB" not in listing
    assert "Nothing tracked in this channel" in actions.list_products(conn, 333)


def test_untrack(conn: sqlite3.Connection) -> None:
    db.add_product(conn, A, "https://shop/x", name="Widget")
    assert actions.untrack(conn, A, 1) == (
        "Stopped tracking #1: Widget\n-# Its price history is kept; `/track` the same URL to resume."
    )
    assert actions.untrack(conn, A, 1) == "No product #1 in this channel"


def test_cannot_untrack_another_channels_product(conn: sqlite3.Connection) -> None:
    db.add_product(conn, A, "https://shop/x", name="Widget")
    assert actions.untrack(conn, B, 1) == "No product #1 in this channel"
    assert db.get_product(conn, 1) is not None


def test_history(conn: sqlite3.Connection) -> None:
    assert actions.history(conn, A, 1) == "No product #1 in this channel"
    db.add_product(conn, A, "https://shop/x", name="Widget")
    assert actions.history(conn, A, 1) == "No prices recorded yet for Shop: [Widget](<https://shop/x>)"
    for price in ("10", "9", "8"):
        actions.check_now(conn, _fetch(price), lambda drop: None, A)
    reply = actions.history(conn, A, 1, limit=2)
    assert reply.splitlines()[0] == "**Shop: [Widget](<https://shop/x>)** (last 2)"
    assert [line.split()[-1] for line in reply.splitlines()[1:]] == ["9.00", "8.00"]


def test_cannot_see_another_channels_history(conn: sqlite3.Connection) -> None:
    db.add_product(conn, A, "https://shop/x", name="Secret")
    assert actions.history(conn, B, 1) == "No product #1 in this channel"


def test_check_now_summarises_each_product(conn: sqlite3.Connection) -> None:
    assert "Nothing tracked" in actions.check_now(conn, _fetch(), _no_notify, A)
    db.add_product(conn, A, "https://good", name="Good")
    db.add_product(conn, A, "https://bad", name="Bad")
    sent: list[PriceDrop] = []

    def fetch(url: str, **_: object) -> str:
        if url == "https://bad":
            raise httpx.ConnectError("down")
        return _page(price)

    price = "20"
    actions.check_now(conn, fetch, sent.append, A)
    price = "15"
    lines = actions.check_now(conn, fetch, sent.append, A).splitlines()
    # Bad was never checked successfully, so it's checked first, but lines stay in /list order.
    assert lines[0] == "📉 #1 Good: 15.00"
    assert lines[1].startswith("❌ #2 Bad: fetch failed")


def test_check_now_with_cutoff_only_checks_stale_products(conn: sqlite3.Connection) -> None:
    # The first scheduled check after a restart: prices checked within the interval are still fresh.
    db.add_product(conn, A, "https://shop/fresh", name="Fresh")
    db.add_product(conn, B, "https://shop/stale", name="Stale")
    db.add_product(conn, A, "https://shop/never", name="Never")
    now = datetime.now(UTC)
    for url, hours_ago in [("https://shop/fresh", 1), ("https://shop/stale", 13)]:
        at = now - timedelta(hours=hours_ago)
        conn.execute("UPDATE products SET last_checked_at = ? WHERE url = ?", (at.isoformat(), url))
    fetched: list[str] = []

    def fetch(url: str, **_: object) -> str:
        fetched.append(url)
        return _page("5")

    cutoff = now - timedelta(hours=12)
    reply = actions.check_now(conn, fetch, _no_notify, None, cutoff)
    assert fetched == ["https://shop/never", "https://shop/stale"]
    assert "Fresh" not in reply

    fetched.clear()
    # Restarted again right away: the run above just checked the rest, so nothing is due.
    reply = actions.check_now(conn, fetch, _no_notify, None, cutoff)
    assert reply == "Nothing due: every product was checked recently."
    assert fetched == []


def test_check_now_only_checks_own_channel(conn: sqlite3.Connection) -> None:
    db.add_product(conn, A, "https://shop/a", name="InA")
    db.add_product(conn, B, "https://shop/b", name="InB")
    fetched: list[str] = []

    def fetch(url: str, **_: object) -> str:
        fetched.append(url)
        return _page("5")

    reply = actions.check_now(conn, fetch, _no_notify, A)
    assert fetched == ["https://shop/a"] and "InB" not in reply

    fetched.clear()
    actions.check_now(conn, fetch, _no_notify, None)  # scheduled check: every channel
    assert fetched == ["https://shop/b", "https://shop/a"]  # b was never checked, so it goes first


def test_check_one_checks_only_that_product(conn: sqlite3.Connection) -> None:
    db.add_product(conn, A, "https://shop/a1", name="First")
    db.add_product(conn, A, "https://shop/a2", name="Second")
    db.add_product(conn, B, "https://shop/b1", name="Other")
    fetched: list[str] = []

    def fetch(url: str, **_: object) -> str:
        fetched.append(url)
        return _page(price)

    price = "20"
    assert actions.check_one(conn, fetch, _no_notify, A, 2) == "✅ #2 Second: 20.00"
    assert fetched == ["https://shop/a2"]
    # Recorded like any check: it's the baseline the next one compares against.
    sent: list[PriceDrop] = []
    price = "15"
    assert actions.check_one(conn, fetch, sent.append, A, 2) == "📉 #2 Second: 15.00"
    assert [d.new_price for d in sent] == [Decimal("15")]
    assert actions.check_one(conn, fetch, _no_notify, B, 2) == "No product #2 in this channel"
    assert fetched == ["https://shop/a2", "https://shop/a2"]


def test_check_one_ignores_block_back_off(conn: sqlite3.Connection) -> None:
    # Asked for explicitly, so it fetches now; only the scheduled check and the full /check wait out a block.
    db.add_product(conn, A, "https://www.acme.com/s/x", name="Widget")
    conn.execute("UPDATE products SET blocked_streak = 2, skips_left = 2")
    assert actions.check_one(conn, _fetch(), _no_notify, A, 1) == "✅ #1 Widget: 63.00"
    product = db.find_product(conn, A, 1)
    assert product is not None and (product.blocked_streak, product.skips_left) == (0, 0)  # success resets it


def test_long_list_fits_discord_limit_and_keeps_code_block(conn: sqlite3.Connection) -> None:
    for i in range(100):
        db.add_product(conn, A, f"https://shop/{i}", name="x" * 40)
    reply = actions.list_products(conn, A)
    assert len(reply) <= actions.MAX_MESSAGE_LENGTH
    block, note, footer = reply.rsplit("\n", 2)
    assert block.startswith("```\n") and block.endswith("\n```")
    shown = len(block.splitlines()) - 4  # fences, header, separator
    assert note == f"…and {100 - shown} more"
    assert footer.startswith("-# ")


def test_track_stores_render_flag(conn: sqlite3.Connection) -> None:
    actions.track(conn, _fetch(), _no_notify, A, "https://shop/x", render=True)
    product = db.get_product(conn, 1)
    assert product is not None and product.render is True


def test_track_reply_uses_page_name_and_notes_browser_switch(conn: sqlite3.Connection) -> None:
    named = (
        '<script type="application/ld+json">'
        '{"@type": "Product", "name": "Deluxe Widget", "offers": {"price": "198.00"}}</script>'
    )

    def fetch(url: str, *, render: bool = False, wait_for: str | None = None) -> str:
        return named if render else "<html>bot check</html>"

    reply = actions.track(conn, fetch, _no_notify, A, "https://shop/x")
    assert reply.splitlines()[:2] == ["Tracking #1: Deluxe Widget", "Current price: **198.00**"]
    assert "headless browser" in reply.splitlines()[2]


def test_track_rejects_options_pasted_into_url(conn: sqlite3.Connection) -> None:
    url = "https://shop/x?a=1 render:True selector:#price .now"
    assert actions.track(conn, _fetch(), _no_notify, A, url).startswith("URL can't contain spaces")
    assert db.list_products(conn) == []


def test_long_check_summary_fits_discord_limit(conn: sqlite3.Connection) -> None:
    for i in range(80):
        db.add_product(conn, A, f"https://shop/{i}", name="y" * 60)
    reply = actions.check_now(conn, _fetch(), _no_notify, A)
    assert len(reply) <= actions.MAX_MESSAGE_LENGTH
    *rows, note = reply.splitlines()
    assert all(row.startswith("✅ #") for row in rows)
    assert note == f"…and {80 - len(rows)} more"


def test_ids_are_per_channel_and_commands_use_them(conn: sqlite3.Connection) -> None:
    for url in ("https://shop/a1", "https://shop/a2"):
        actions.track(conn, _fetch(), _no_notify, A, url)
    actions.track(conn, _fetch(), _no_notify, B, "https://shop/b1", name="B-one")

    # B's first product is #1 there, even though it's the third product overall.
    assert "B-one" in actions.history(conn, B, 1)
    assert actions.history(conn, B, 2) == "No product #2 in this channel"
    assert actions.untrack(conn, A, 1).startswith("Stopped tracking #1: https://shop/a1\n")
    assert actions.untrack(conn, B, 1).startswith("Stopped tracking #1: B-one\n")
    assert [p.url for p in db.list_products(conn, A)] == ["https://shop/a2"]  # A's #2 is untouched


def test_check_summary_uses_channel_ids(conn: sqlite3.Connection) -> None:
    db.add_product(conn, A, "https://shop/a", name="InA")
    db.add_product(conn, B, "https://shop/b", name="InB")
    assert actions.check_now(conn, _fetch(), _no_notify, B) == "✅ #1 InB: 63.00"


def test_check_summary_shows_skipped_products(conn: sqlite3.Connection) -> None:
    db.add_product(conn, A, "https://www.acme.com/s/x", name="Widget")
    conn.execute("UPDATE products SET blocked_streak = 2, skips_left = 2")
    assert actions.check_now(conn, _fetch(), _no_notify, A) == (
        "⏸️ #1 Widget: skipped, Acme blocked us; retrying in 2 checks"
    )
    assert actions.check_now(conn, _fetch(), _no_notify, A) == (
        "⏸️ #1 Widget: skipped, Acme blocked us; retrying in next check"
    )
    assert actions.check_now(conn, _fetch(), _no_notify, A) == "✅ #1 Widget: 63.00"


@pytest.mark.parametrize(
    ("raw", "current", "expected"),
    [
        ("150", None, (Decimal("150"), None)),
        (" $149.99 ", None, (Decimal("149.99"), None)),
        ("1.299,00 €", None, (Decimal("1299.00"), None)),
        ("-20%", Decimal("96.00"), (Decimal("76.80"), Decimal("20"))),
        ("- 12.5 %", Decimal("199.95"), (Decimal("174.96"), Decimal("12.5"))),  # 174.95625 rounds half-up
        ("-33.333%", Decimal("10"), (Decimal("6.67"), Decimal("33.333"))),
    ],
)
def test_parse_target(raw: str, current: Decimal | None, expected: tuple[Decimal, Decimal | None]) -> None:
    assert actions.parse_target(raw, current) == expected


@pytest.mark.parametrize(
    ("raw", "current", "message_start"),
    [
        ("20%", Decimal("100"), "Use a negative percentage"),
        ("+20%", Decimal("100"), "Use a negative percentage"),
        ("-100%", Decimal("100"), "The percentage must be above 0"),
        ("-0%", Decimal("100"), "The percentage must be above 0"),
        ("-20%", None, "There's no current price yet"),
        ("-5", None, "`-5` isn't a price"),  # parse_price alone would read this as 5
        ("cheap", None, "`cheap` isn't a price"),
        ("0", None, "`0` isn't a price"),
    ],
)
def test_parse_target_rejects(raw: str, current: Decimal | None, message_start: str) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(message_start)}"):
        actions.parse_target(raw, current)


def _tracked_at(conn: sqlite3.Connection, price: str, **kwargs: object) -> None:
    product = db.add_product(conn, A, "https://shop/x", name="Widget", **kwargs)  # type: ignore[arg-type]
    db.record_price(conn, product, Decimal(price), None)


def test_set_target_amount(conn: sqlite3.Connection) -> None:
    _tracked_at(conn, "96.00")
    assert actions.set_target(conn, A, 1, "80") == "Target for #1 Widget set to **80.00**"
    product = db.find_product(conn, A, 1)
    assert product is not None and product.target_price == Decimal("80")


def test_set_target_percentage(conn: sqlite3.Connection) -> None:
    _tracked_at(conn, "96.00")
    assert actions.set_target(conn, A, 1, "-20%") == (
        "Target for #1 Widget set to **76.80** (20% below the current 96.00)"
    )


def test_set_target_at_or_above_current_warns_of_alert(conn: sqlite3.Connection) -> None:
    _tracked_at(conn, "96.00")
    reply = actions.set_target(conn, A, 1, "100")
    assert reply.endswith("The current price is already at or below it, so you'll get an alert at the next check.")


def test_clear_target(conn: sqlite3.Connection) -> None:
    _tracked_at(conn, "96.00", target_price=Decimal("50"))
    assert actions.set_target(conn, A, 1, None) == (
        "Cleared the target for #1 Widget; you'll be alerted on any price drop."
    )
    assert actions.set_target(conn, A, 1, "  ").startswith("Cleared")
    product = db.find_product(conn, A, 1)
    assert product is not None and product.target_price is None


def test_set_target_errors_are_replies_and_change_nothing(conn: sqlite3.Connection) -> None:
    _tracked_at(conn, "96.00", target_price=Decimal("50"))
    assert actions.set_target(conn, A, 1, "20%").startswith("Use a negative percentage")
    assert actions.set_target(conn, A, 2, "80") == "No product #2 in this channel"
    assert actions.set_target(conn, B, 1, "80") == "No product #1 in this channel"  # other channel's #1
    product = db.find_product(conn, A, 1)
    assert product is not None and product.target_price == Decimal("50")


def test_new_target_is_judged_fresh_at_next_check(conn: sqlite3.Connection) -> None:
    # Alerted at 40 under target 50; raising the target to 45 must alert again even though 40 was already sent.
    sent: list[PriceDrop] = []
    actions.track(conn, _fetch("40"), sent.append, A, "https://shop/x", target=Decimal("50"))
    actions.check_now(conn, _fetch("40"), sent.append, A)
    assert len(sent) == 1  # no repeat under the old target

    actions.set_target(conn, A, 1, "45")
    actions.check_now(conn, _fetch("40"), sent.append, A)
    assert [d.reason for d in sent] == ["At or below your target of 50.00", "At or below your target of 45.00"]


def test_history_header_names_the_store(conn: sqlite3.Connection) -> None:
    product = db.add_product(conn, A, "https://www.acme.com/s/x", name="Deluxe Widget")
    db.record_price(conn, product, Decimal("198"), None)
    assert actions.history(conn, A, 1).splitlines()[0] == (
        "**Acme: [Deluxe Widget](<https://www.acme.com/s/x>)** (last 1)"
    )


def test_untracked_products_are_hidden_everywhere(conn: sqlite3.Connection) -> None:
    db.add_product(conn, A, "https://shop/keep", name="Keep")
    db.add_product(conn, A, "https://shop/gone", name="Gone")
    actions.untrack(conn, A, 2)

    assert "Gone" not in actions.list_products(conn, A)
    assert actions.history(conn, A, 2) == "No product #2 in this channel"
    assert actions.set_target(conn, A, 2, "10") == "No product #2 in this channel"
    assert actions.untrack(conn, A, 2) == "No product #2 in this channel"
    assert actions.check_one(conn, _fetch(), _no_notify, A, 2) == "No product #2 in this channel"
    fetched: list[str] = []

    def fetch(url: str, **_: object) -> str:
        fetched.append(url)
        return _page("5")

    actions.check_now(conn, fetch, lambda drop: None, None)  # the scheduled check, all channels
    assert fetched == ["https://shop/keep"]


def test_tracking_an_untracked_url_restores_it(conn: sqlite3.Connection) -> None:
    actions.track(conn, _fetch("100"), _no_notify, A, "https://shop/a1")
    actions.track(conn, _fetch("100"), _no_notify, A, "https://shop/x", name="Widget")
    actions.untrack(conn, A, 2)

    reply = actions.track(conn, _fetch("90"), lambda drop: None, A, "https://shop/x", target=Decimal("95"))
    assert reply.splitlines()[0] == "Tracking #2: Widget (tracked before; its ID and price history are back)"
    product = db.find_product(conn, A, 2)
    assert product is not None and product.target_price == Decimal("95")
    assert [p for p, _ in db.price_history(conn, product.id)] == [Decimal("100"), Decimal("90")]


def test_untracked_url_in_another_channel_is_tracked_fresh(conn: sqlite3.Connection) -> None:
    actions.track(conn, _fetch(), _no_notify, A, "https://shop/x")
    actions.untrack(conn, A, 1)
    reply = actions.track(conn, _fetch(), _no_notify, B, "https://shop/x")
    assert reply.splitlines()[0] == "Tracking #1: https://shop/x"  # no "tracked before"
