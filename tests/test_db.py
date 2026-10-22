import re
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from price_tracker import db


def test_add_and_get_round_trips_decimals(conn: sqlite3.Connection) -> None:
    pid = db.add_product(conn, 1, "https://shop/x", name="X", target_price=Decimal("19.90"), selector=".p").id
    product = db.get_product(conn, pid)
    assert product is not None
    assert product.target_price == Decimal("19.90")
    assert str(product.target_price) == "19.90"  # trailing zero preserved: stored as TEXT, not REAL
    assert (product.name, product.selector, product.last_price) == ("X", ".p", None)


def test_duplicate_url_rejected(conn: sqlite3.Connection) -> None:
    db.add_product(conn, 1, "https://shop/x")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_product(conn, 1, "https://shop/x")


def test_record_price_tracks_lowest_and_history(conn: sqlite3.Connection) -> None:
    pid = db.add_product(conn, 1, "https://shop/x").id
    for price in ("50", "40", "45"):
        product = db.get_product(conn, pid)
        assert product is not None
        db.record_price(conn, product, Decimal(price), last_notified_price=None)

    product = db.get_product(conn, pid)
    assert product is not None
    assert product.last_price == Decimal("45")
    assert product.lowest_price == Decimal("40")
    assert product.last_checked_at is not None
    assert [p for p, _ in db.price_history(conn, pid)] == [Decimal("50"), Decimal("40"), Decimal("45")]


def test_remove_cascades_history(conn: sqlite3.Connection) -> None:
    pid = db.add_product(conn, 1, "https://shop/x").id
    product = db.get_product(conn, pid)
    assert product is not None
    db.record_price(conn, product, Decimal("10"), None)

    assert db.remove_product(conn, pid) is True
    assert db.remove_product(conn, pid) is False
    assert conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0


def test_data_persists_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    first = db.connect(path)
    db.add_product(first, 1, "https://shop/x")
    first.close()

    second = db.connect(path)
    assert [p.url for p in db.list_products(second)] == ["https://shop/x"]
    second.close()


def test_render_round_trips(conn: sqlite3.Connection) -> None:
    plain = db.add_product(conn, 1, "https://shop/plain").id
    rendered = db.add_product(conn, 1, "https://shop/js", render=True).id
    assert [db.get_product(conn, i).render for i in (plain, rendered)] == [False, True]  # type: ignore[union-attr]


def _v1_database(path: Path) -> None:
    """A database as the render release left it: url UNIQUE, render column, no channel_id, user_version 1."""
    old = sqlite3.connect(path)
    old.executescript(db._SCHEMA)
    for statement in db._MIGRATIONS[0]:
        old.execute(statement)
    old.execute("PRAGMA user_version = 1")
    old.execute(
        "INSERT INTO products (url, name, render, last_price, created_at) VALUES (?, 'X', 1, '96.00', ?)",
        ("https://shop/x", "2026-10-22T12:00:00+00:00"),
    )
    old.execute(
        "INSERT INTO price_history (product_id, price, checked_at) VALUES (1, '96', '2026-10-22T12:00:00+00:00')"
    )
    old.commit()
    old.close()


def test_channel_migration_drops_unattributable_products(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    _v1_database(path)

    conn = db.connect(path)
    assert db.list_products(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db._MIGRATIONS)
    with pytest.raises(sqlite3.IntegrityError):  # channel_id is NOT NULL now
        conn.execute("INSERT INTO products (url, created_at) VALUES ('https://shop/y', 't')")
    # The rebuilt table is still the FK parent of price_history: deleting a product cascades.
    pid = db.add_product(conn, 1, "https://shop/x").id
    product = db.get_product(conn, pid)
    assert product is not None
    db.record_price(conn, product, Decimal("10"), None)
    db.remove_product(conn, pid)
    assert conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0
    conn.close()

    db.connect(path).close()  # reconnecting must not re-run migrations


def test_migrations_run_with_foreign_keys_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A future rebuild migration (copy, DROP, RENAME) must not cascade-delete history on the DROP.
    path = tmp_path / "t.db"
    conn = db.connect(path)
    product = db.add_product(conn, 1, "https://shop/x")
    assert product is not None
    db.record_price(conn, product, Decimal("10"), None)
    [create_sql] = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'products'").fetchone()
    conn.close()

    rebuild = (
        # Same definition (keeps the PRIMARY KEY); SQLite quotes the name after a RENAME, hence the regex.
        re.sub(r'CREATE TABLE "?products"?', "CREATE TABLE products_new", create_sql, count=1),
        "INSERT INTO products_new SELECT * FROM products",
        "DROP TABLE products",
        "ALTER TABLE products_new RENAME TO products",
    )
    monkeypatch.setattr(db, "_MIGRATIONS", (*db._MIGRATIONS, rebuild))
    conn = db.connect(path)
    assert [p for p, _ in db.price_history(conn, 1)] == [Decimal("10")]
    conn.close()


def test_same_url_allowed_in_different_channels(conn: sqlite3.Connection) -> None:
    db.add_product(conn, 1, "https://shop/x")
    db.add_product(conn, 2, "https://shop/x")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_product(conn, 1, "https://shop/x")


def test_queries_scope_to_channel(conn: sqlite3.Connection) -> None:
    a = db.add_product(conn, 1, "https://shop/a").id
    b = db.add_product(conn, 2, "https://shop/b").id
    c = db.add_product(conn, 3, "https://shop/c").id

    assert [p.id for p in db.list_products(conn, 1)] == [a]
    assert [p.id for p in db.list_products(conn)] == [a, b, c]  # None = every channel
    assert db.find_product(conn, 1, 1) is not None
    assert db.find_product(conn, 1, 2) is None  # channel 2's product isn't reachable from channel 1


def test_numbers_are_per_channel_and_stable(conn: sqlite3.Connection) -> None:
    numbers = [db.add_product(conn, ch, f"https://shop/{ch}/{i}").number for ch, i in [(1, 1), (2, 1), (1, 2), (1, 3)]]
    assert numbers == [1, 1, 2, 3]

    db.remove_product(conn, db.find_product(conn, 1, 2).id)  # type: ignore[union-attr]
    assert [p.number for p in db.list_products(conn, 1)] == [1, 3]  # no renumbering
    assert db.add_product(conn, 1, "https://shop/1/4").number == 4

    with pytest.raises(sqlite3.IntegrityError):  # (channel_id, number) is unique
        conn.execute("UPDATE products SET number = 1 WHERE channel_id = 1 AND number = 3")


def test_number_migration_backfills_in_creation_order(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    old = sqlite3.connect(path)
    old.executescript(db._SCHEMA)
    for migration in db._MIGRATIONS[:2]:
        for statement in migration:
            old.execute(statement)
    old.execute("PRAGMA user_version = 2")
    for channel, url in [(10, "a"), (20, "b"), (10, "c"), (20, "d"), (10, "e")]:
        old.execute("INSERT INTO products (channel_id, url, created_at) VALUES (?, ?, 't')", (channel, url))
    old.commit()
    old.close()

    conn = db.connect(path)
    assert [(p.url, p.number) for p in db.list_products(conn, 10)] == [("a", 1), ("c", 2), ("e", 3)]
    assert [(p.url, p.number) for p in db.list_products(conn, 20)] == [("b", 1), ("d", 2)]
    assert db.add_product(conn, 10, "f").number == 4
    conn.close()
