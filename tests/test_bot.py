import asyncio
import random
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from discord import app_commands

from price_tracker import actions, db
from price_tracker.bot import create_bot, jittered_hours
from price_tracker.config import Settings


def _commands() -> dict[str, app_commands.Command]:  # type: ignore[type-arg]
    bot = create_bot(Settings(discord_bot_token=None, discord_guild_id=None), token="unused")
    return {c.name: c for c in bot.tree.get_commands() if isinstance(c, app_commands.Command)}


def test_registers_slash_commands() -> None:
    assert set(_commands()) == {"track", "list", "untrack", "history", "target", "check"}


def test_every_command_is_owner_only() -> None:
    for name, cmd in _commands().items():
        assert len(cmd.checks) == 1, name


def test_id_option_is_named_id() -> None:
    for name in ("untrack", "history", "target"):
        param = _commands()[name].parameters[0]
        assert (param.display_name, param.required) == ("id", True)


def test_target_price_option_is_optional() -> None:
    [_, price] = _commands()["target"].parameters
    assert (price.name, price.required) == ("price", False)
    assert len(price.description) <= 100  # Discord's limit for option descriptions


def test_track_parameters() -> None:
    params = {p.name: p for p in _commands()["track"].parameters}
    assert params["url"].required
    assert not any(params[n].required for n in ("name", "target", "selector", "render"))
    assert params["target"].description == "Only alert at or below this price"


class _FakeInteraction:
    """Records what a command sends: `events` holds "deferred" and each reply, in order."""

    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id
        self.events: list[str] = []
        self.response = self
        self.followup = self

    async def defer(self, thinking: bool = False) -> None:
        self.events.append("deferred")

    async def send(self, message: str, ephemeral: bool = False) -> None:
        self.events.append(message)

    async def send_message(self, message: str, ephemeral: bool = False) -> None:
        self.events.append(message)

    @property
    def replies(self) -> list[str]:
        return [e for e in self.events if e != "deferred"]


def _callback(bot: Any, command: str) -> Any:
    """A command's callback, typed loosely so tests can pass a fake interaction."""
    return bot.tree.get_command(command).callback


def _call(bot: Any, command: str, channel_id: int, **kwargs: object) -> str:
    """Invoke a slash command's callback as if it were used in `channel_id`; return the reply."""
    interaction = _FakeInteraction(channel_id)
    asyncio.run(_callback(bot, command)(interaction, **kwargs))
    assert interaction.events[0] == "deferred"  # acknowledged before doing any work
    [reply] = interaction.replies
    return reply


def test_commands_are_scoped_to_the_invoking_channel(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "bot.db", discord_bot_token=None, discord_guild_id=None)
    conn = db.connect(settings.db_path)
    db.add_product(conn, 111, "https://shop/a", name="Alpha")
    db.add_product(conn, 222, "https://shop/b", name="Bravo")
    conn.close()
    bot = create_bot(settings, token="unused")

    listing = _call(bot, "list", 111)
    assert "Alpha" in listing and "Bravo" not in listing
    # Each channel has its own #1.
    assert "Alpha" in _call(bot, "history", 111, number=1)
    assert _call(bot, "untrack", 111, number=2) == "No product #2 in this channel"
    assert _call(bot, "untrack", 222, number=1).startswith("Stopped tracking #1: Bravo\n")
    assert "Alpha" in _call(bot, "list", 111)  # untouched by 222's /untrack 1


def test_commands_acknowledge_immediately_while_a_check_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: /list during a slow scheduled check waited on the lock past Discord's 3 s reply deadline.
    settings = Settings(db_path=tmp_path / "bot.db", discord_bot_token=None, discord_guild_id=None)
    conn = db.connect(settings.db_path)
    db.add_product(conn, 111, "https://shop/a", name="Alpha")
    conn.close()
    release = threading.Event()

    def slow_check(conn: object, fetch: object, notify: object, channel_id: int | None) -> str:
        release.wait(timeout=5)
        return "check done"

    monkeypatch.setattr(actions, "check_now", slow_check)
    bot = create_bot(settings, token="unused")

    async def scenario() -> None:
        check = _FakeInteraction(111)
        check_task = asyncio.create_task(_callback(bot, "check")(check))
        await asyncio.sleep(0.1)  # the check now holds the lock

        listing, untrack = _FakeInteraction(111), _FakeInteraction(111)
        list_task = asyncio.create_task(_callback(bot, "list")(listing))
        untrack_task = asyncio.create_task(_callback(bot, "untrack")(untrack, number=1))
        await asyncio.wait_for(list_task, timeout=2)  # reads don't wait for the lock

        assert "Alpha" in listing.replies[0]
        assert untrack.events == ["deferred"]  # acknowledged, but queued behind the check

        release.set()
        await asyncio.wait_for(asyncio.gather(check_task, untrack_task), timeout=5)
        assert check.replies == ["check done"]
        [reply] = untrack.replies
        assert reply.startswith("Stopped tracking #1: Alpha\n")

    asyncio.run(scenario())


def test_check_id_option_is_optional() -> None:
    [param] = _commands()["check"].parameters
    assert (param.display_name, param.required) == ("id", False)


def test_check_without_id_checks_the_channel_and_with_id_one_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(db_path=tmp_path / "bot.db", discord_bot_token=None, discord_guild_id=None)
    calls: list[tuple[str, int, int | None]] = []

    def check_now(conn: object, fetch: object, notify: object, channel_id: int) -> str:
        calls.append(("all", channel_id, None))
        return "all checked"

    def check_one(conn: object, fetch: object, notify: object, channel_id: int, number: int) -> str:
        calls.append(("one", channel_id, number))
        return "one checked"

    monkeypatch.setattr(actions, "check_now", check_now)
    monkeypatch.setattr(actions, "check_one", check_one)
    bot = create_bot(settings, token="unused")

    assert _call(bot, "check", 111) == "all checked"
    assert _call(bot, "check", 111, number=3) == "one checked"
    assert calls == [("all", 111, None), ("one", 111, 3)]


def test_jittered_hours_stays_within_ten_percent() -> None:
    rng = random.Random(1)
    samples = [jittered_hours(12, rng) for _ in range(200)]
    assert all(10.8 <= h <= 13.2 for h in samples)
    assert max(samples) - min(samples) > 1.5  # actually varies


def test_target_command_sets_and_clears(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "bot.db", discord_bot_token=None, discord_guild_id=None)
    conn = db.connect(settings.db_path)
    product = db.add_product(conn, 111, "https://shop/a", name="Alpha")
    db.record_price(conn, product, Decimal("100"), None)
    conn.close()
    bot = create_bot(settings, token="unused")

    assert _call(bot, "target", 111, number=1, price="-20%").startswith("Target for #1 Alpha set to **80.00**")
    assert _call(bot, "target", 222, number=1, price="50") == "No product #1 in this channel"
    assert _call(bot, "target", 111, number=1).startswith("Cleared the target for #1 Alpha")


def test_track_rejects_percentage_target_before_any_work() -> None:
    bot = create_bot(Settings(discord_bot_token=None, discord_guild_id=None), token="unused")
    interaction = _FakeInteraction(111)
    asyncio.run(_callback(bot, "track")(interaction, url="https://shop/x", target="-20%"))
    [reply] = interaction.events  # answered directly; never deferred, nothing fetched
    assert reply.startswith("There's no current price yet")
