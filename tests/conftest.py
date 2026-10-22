import sqlite3
from collections.abc import Iterator

import pytest

from price_tracker import db, tracker


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def no_store_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Many tests use several products on one fake store; real 3-8 s gaps would make the suite crawl.
    # Pacing itself is tested with an explicit gap and a fake clock.
    monkeypatch.setattr(tracker, "SAME_STORE_GAP_SECONDS", (0.0, 0.0))
