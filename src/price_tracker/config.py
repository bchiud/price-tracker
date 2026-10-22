from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings, read from environment variables or a `.env` file.

    Field names map to env vars case-insensitively, e.g. `check_interval_hours` <- `CHECK_INTERVAL_HOURS`.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    check_interval_hours: float = Field(default=12.0, gt=0)
    db_path: Path = Path("price_tracker.db")
    request_timeout_seconds: float = Field(default=20.0, gt=0)
    # Required. SecretStr keeps it out of reprs and logs; Optional only so a missing token gets a friendly error.
    discord_bot_token: SecretStr | None = None
    # Set to your server's ID so slash commands appear instantly; global sync can take a while.
    discord_guild_id: int | None = None

    @field_validator("discord_bot_token", "discord_guild_id", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `DISCORD_GUILD_ID=` (blank line in .env) should mean "not configured", not an empty value.
        return None if isinstance(value, str) and not value.strip() else value
