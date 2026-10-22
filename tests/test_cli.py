from pathlib import Path

import discord
import pytest

from price_tracker import cli


class _FakeBot:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.ran_with: str | None = None

    def run(self, token: str, log_handler: object = None) -> None:
        self.ran_with = token
        if self.error:
            raise self.error


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)


def test_requires_bot_token(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert cli.main([]) == 1
    assert "DISCORD_BOT_TOKEN is not set" in capsys.readouterr().err


def test_migrates_then_runs_bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    bot = _FakeBot()
    monkeypatch.setattr(cli, "create_bot", lambda settings, token: bot)
    assert cli.main([]) == 0
    assert bot.ran_with == "tok"
    assert (tmp_path / "cli.db").exists()


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        (discord.LoginFailure("bad"), "rejected DISCORD_BOT_TOKEN"),
        (discord.Forbidden(type("R", (), {"status": 403, "reason": "Forbidden"})(), "Missing Access"), "invited with"),
    ],
)
def test_friendly_startup_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception, hint: str
) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setattr(cli, "create_bot", lambda settings, token: _FakeBot(error))
    assert cli.main([]) == 1
    assert hint in capsys.readouterr().err
