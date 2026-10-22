from pathlib import Path

import pytest
from pydantic import ValidationError

from price_tracker.config import Settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Run from an empty dir so a developer's real .env can't leak into these tests.
    monkeypatch.chdir(tmp_path)
    for var in ("CHECK_INTERVAL_HOURS", "DB_PATH", "DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "REQUEST_TIMEOUT_SECONDS"):
        monkeypatch.delenv(var, raising=False)


def test_defaults() -> None:
    settings = Settings()
    assert settings.check_interval_hours == 12.0
    assert settings.discord_bot_token is None
    assert settings.discord_guild_id is None
    assert settings.db_path == Path("price_tracker.db")


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECK_INTERVAL_HOURS", "0.5")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123456789012345678")
    settings = Settings()
    assert settings.check_interval_hours == 0.5
    assert settings.discord_guild_id == 123456789012345678
    assert settings.discord_bot_token is not None
    assert settings.discord_bot_token.get_secret_value() == "secret-token"
    assert "secret-token" not in repr(settings)


def test_reads_dotenv_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("CHECK_INTERVAL_HOURS=6\n")
    assert Settings().check_interval_hours == 6


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_values_are_unset(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", value)
    monkeypatch.setenv("DISCORD_GUILD_ID", value)  # would fail int parsing without the validator
    settings = Settings()
    assert (settings.discord_bot_token, settings.discord_guild_id) == (None, None)


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_rejects_invalid_interval(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CHECK_INTERVAL_HOURS", value)
    with pytest.raises(ValidationError):
        Settings()
