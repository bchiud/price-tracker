"""Discord bot: slash commands to manage tracked products, plus the scheduled price check.

One process does both, so checks and commands never race on the same product (see `lock`).
Each command is scoped to the channel it was used in, and alerts are posted back to the product's own channel.

discord.py is async; our DB/HTTP code is sync. Each operation runs in a worker thread via `asyncio.to_thread`
with its own SQLite connection, because sqlite3 connections can't be shared across threads by default.
"""

import asyncio
import logging
import random
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import discord
import httpx
from discord import app_commands
from discord.ext import commands, tasks

from price_tracker import actions, db
from price_tracker.config import Settings
from price_tracker.fetch import PageFetcher
from price_tracker.notify import ChannelNotifier
from price_tracker.tracker import Fetcher, Notifier

log = logging.getLogger(__name__)

# Work to run in a worker thread, given its own DB connection, page fetcher, and alert notifier.
type Job[T] = Callable[[sqlite3.Connection, Fetcher, Notifier], T]


def create_bot(settings: Settings, token: str) -> commands.Bot:
    """`token` is the bot token; besides logging in, it authenticates the alert posts to each channel."""
    # Slash commands need no privileged intents (message content etc.).
    bot = commands.Bot(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
    lock = asyncio.Lock()

    def run_sync[T](fn: Job[T]) -> T:
        conn = db.connect(settings.db_path)
        try:
            with (
                httpx.Client(timeout=settings.request_timeout_seconds) as client,  # Discord API only
                PageFetcher(settings.request_timeout_seconds) as fetch,
            ):
                return fn(conn, fetch, ChannelNotifier(client, token))
        finally:
            conn.close()

    async def run[T](fn: Job[T], *, exclusive: bool = True) -> T:
        """`exclusive` (the default) serializes anything that fetches or writes, so a command and the scheduled check
        never race on the same product. Read-only work skips the lock: each call opens its own SQLite connection."""
        if not exclusive:
            return await asyncio.to_thread(run_sync, fn)
        async with lock:
            return await asyncio.to_thread(run_sync, fn)

    async def respond(
        interaction: discord.Interaction,
        fn: Job[str],
        *,
        exclusive: bool = True,
    ) -> None:
        """Acknowledge at once, then reply when `fn` finishes. Discord drops an interaction that isn't answered
        within 3 seconds, and the work can be slow (page fetches) or queued behind the scheduled check's lock."""
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(await run(fn, exclusive=exclusive))

    async def owner_only(interaction: discord.Interaction) -> bool:
        # Bot.is_owner resolves the application owner (or team members) from the Developer Portal.
        return await bot.is_owner(interaction.user)

    @bot.tree.command(name="track", description="Start tracking a product URL")
    @app_commands.check(owner_only)
    @app_commands.describe(
        url="Product page URL",
        name="Display name (defaults to the product name on the page)",
        target="Only alert at or below this price",
        selector="CSS selector for the price; overrides the page's structured data",
        render="Always use a headless browser, e.g. with a selector for a sale price a script draws",
    )
    async def track(
        interaction: discord.Interaction,
        url: str,
        name: str | None = None,
        target: str | None = None,
        selector: str | None = None,
        render: bool = False,
    ) -> None:
        target_price = None
        if target is not None:
            try:
                # No current price before the first check, so a percentage target is rejected with a hint.
                target_price, _ = actions.parse_target(target, current=None)
            except ValueError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return
        channel_id = _channel_id(interaction)
        await respond(
            interaction,
            lambda conn, fetch, notify: actions.track(
                conn, fetch, notify, channel_id, url, name, target_price, selector, render
            ),
        )

    @bot.tree.command(name="list", description="Show products tracked in this channel")
    @app_commands.check(owner_only)
    async def list_(interaction: discord.Interaction) -> None:
        channel_id = _channel_id(interaction)
        await respond(interaction, lambda conn, *_: actions.list_products(conn, channel_id), exclusive=False)

    @bot.tree.command(name="untrack", description="Stop tracking a product")
    @app_commands.check(owner_only)
    @app_commands.rename(number="id")
    @app_commands.describe(number="ID from /list")
    async def untrack(interaction: discord.Interaction, number: int) -> None:
        channel_id = _channel_id(interaction)
        await respond(interaction, lambda conn, *_: actions.untrack(conn, channel_id, number))

    @bot.tree.command(name="history", description="Show a product's recent prices")
    @app_commands.check(owner_only)
    @app_commands.rename(number="id")
    @app_commands.describe(number="ID from /list")
    async def history(interaction: discord.Interaction, number: int) -> None:
        channel_id = _channel_id(interaction)
        await respond(interaction, lambda conn, *_: actions.history(conn, channel_id, number), exclusive=False)

    @bot.tree.command(name="target", description="Set or clear a product's target price")
    @app_commands.check(owner_only)
    @app_commands.rename(number="id")
    @app_commands.describe(
        number="ID from /list",
        price="e.g. 150, or -20% for 20% below the current price; leave empty to clear",
    )
    async def target(interaction: discord.Interaction, number: int, price: str | None = None) -> None:
        channel_id = _channel_id(interaction)
        await respond(interaction, lambda conn, *_: actions.set_target(conn, channel_id, number, price))

    @bot.tree.command(name="check", description="Check this channel's prices now, or one product's")
    @app_commands.check(owner_only)
    @app_commands.rename(number="id")
    @app_commands.describe(number="ID from /list; leave empty to check every product")
    async def check(interaction: discord.Interaction, number: int | None = None) -> None:
        channel_id = _channel_id(interaction)
        await respond(
            interaction,
            lambda conn, fetch, notify: (
                actions.check_now(conn, fetch, notify, channel_id)
                if number is None
                else actions.check_one(conn, fetch, notify, channel_id, number)
            ),
        )

    @bot.tree.error
    async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = "Only the bot's owner can use this."
        else:
            log.exception("Command failed", exc_info=error)
            message = "Something went wrong — check the bot's logs."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException as e:
            # e.g. the interaction already expired; raising here would only add a second traceback.
            log.warning("Couldn't report the error to Discord: %s", e)

    @tasks.loop(hours=settings.check_interval_hours)
    async def scheduled_check() -> None:
        # The loop's first run is at startup. A restart shouldn't re-check prices that are still fresh, so that run
        # only takes products last checked over an interval ago (the base one: jitter would make the cutoff random).
        checked_before = (
            datetime.now(UTC) - timedelta(hours=settings.check_interval_hours)
            if scheduled_check.current_loop == 0
            else None
        )
        try:
            # All channels; each product's alert still goes to its own channel.
            summary = await run(
                lambda conn, fetch, notify: actions.check_now(conn, fetch, notify, None, checked_before)
            )
            log.info("Scheduled check:\n%s", summary)
        except Exception:
            # An unhandled exception would stop the loop for good.
            log.exception("Scheduled check failed")
        # Vary the next run so checks don't land at the same clock time every day (an easy automation signal).
        next_hours = jittered_hours(settings.check_interval_hours)
        scheduled_check.change_interval(hours=next_hours)
        log.info("Next check in %.1f hours", next_hours)

    @scheduled_check.before_loop
    async def before_scheduled_check() -> None:
        await bot.wait_until_ready()

    async def setup_hook() -> None:
        if settings.discord_guild_id is not None:
            guild = discord.Object(id=settings.discord_guild_id)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        else:
            synced = await bot.tree.sync()
        log.info("Synced %d slash command(s); checking every %g hours", len(synced), settings.check_interval_hours)
        scheduled_check.start()

    bot.setup_hook = setup_hook  # type: ignore[method-assign]
    return bot


def jittered_hours(base: float, rng: random.Random | None = None, spread: float = 0.1) -> float:
    """`base` ± `spread` (10%), uniformly at random."""
    return (rng or random).uniform(base * (1 - spread), base * (1 + spread))


def _channel_id(interaction: discord.Interaction) -> int:
    # Guild slash commands always carry the channel (or thread) they were used in.
    if interaction.channel_id is None:
        raise RuntimeError("command has no channel")
    return interaction.channel_id
