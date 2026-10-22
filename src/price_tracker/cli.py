import argparse
import logging
import sys
from collections.abc import Sequence

import discord

from price_tracker import db
from price_tracker.bot import create_bot
from price_tracker.config import Settings


def main(argv: Sequence[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="price-tracker",
        description="Run the Discord price-tracking bot. Everything else happens through its slash commands.",
    ).parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = Settings()
    if settings.discord_bot_token is None:
        print("DISCORD_BOT_TOKEN is not set (see README: Discord setup)", file=sys.stderr)
        return 1
    # Open once up front so schema migrations run (and fail loudly) before connecting to Discord.
    db.connect(settings.db_path).close()

    token = settings.discord_bot_token.get_secret_value()
    try:
        # log_handler=None: keep the logging configured above instead of discord.py's own.
        create_bot(settings, token).run(token, log_handler=None)
    except discord.LoginFailure:
        print("Discord rejected DISCORD_BOT_TOKEN — reset it in the Developer Portal (Bot tab).", file=sys.stderr)
        return 1
    except discord.Forbidden as e:
        print(
            f"Discord refused to register slash commands ({e.text}). Check that DISCORD_GUILD_ID is your server's ID "
            "and the bot was invited with the `bot` and `applications.commands` scopes.",
            file=sys.stderr,
        )
        return 1
    return 0
