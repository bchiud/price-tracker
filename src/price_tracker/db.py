"""SQLite persistence. Prices are stored as TEXT so Decimal values round-trip exactly.

Every product belongs to the Discord channel it was tracked from (`channel_id`) and has a `number` there: the ID
users see and type. `id` is the internal key (price history points at it).
"""

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from price_tracker.models import Product

# The original (version 0) schema. _MIGRATIONS bring it to the current shape (channel_id, number, render, per-channel
# URL uniqueness, block back-off); new databases replay them too, so every database reaches the current schema by
# the same path.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id                  INTEGER PRIMARY KEY,
    url                 TEXT NOT NULL UNIQUE,
    name                TEXT,
    target_price        TEXT,
    selector            TEXT,
    last_price          TEXT,
    lowest_price        TEXT,
    last_notified_price TEXT,
    last_checked_at     TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_history (
    id         INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    price      TEXT NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_history_product ON price_history(product_id, checked_at);
"""

# Applied in order to bring older databases up to date; PRAGMA user_version records how many have run.
# Each migration is a tuple of statements run in one transaction. Append only: never change one that has shipped.
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    ("ALTER TABLE products ADD COLUMN render INTEGER NOT NULL DEFAULT 0",),
    # Scope products to the Discord channel they were tracked from. URL uniqueness becomes per channel, and SQLite
    # can't change constraints in place, so rebuild the table (https://sqlite.org/lang_altertable.html#otheralter).
    # Products from before channels existed have no channel to belong to, so they aren't carried over.
    (
        """CREATE TABLE products_new (
            id                  INTEGER PRIMARY KEY,
            channel_id          INTEGER NOT NULL,
            url                 TEXT NOT NULL,
            name                TEXT,
            target_price        TEXT,
            selector            TEXT,
            render              INTEGER NOT NULL DEFAULT 0,
            last_price          TEXT,
            lowest_price        TEXT,
            last_notified_price TEXT,
            last_checked_at     TEXT,
            created_at          TEXT NOT NULL,
            UNIQUE (channel_id, url)
        )""",
        "DELETE FROM price_history",
        "DROP TABLE products",
        "ALTER TABLE products_new RENAME TO products",
    ),
    # Per-channel display numbers (see add_product). Existing products are numbered in the order they were added.
    (
        "ALTER TABLE products ADD COLUMN number INTEGER NOT NULL DEFAULT 0",
        """UPDATE products SET number = (
               SELECT COUNT(*) FROM products AS earlier
               WHERE earlier.channel_id = products.channel_id AND earlier.id <= products.id
           )""",
        "CREATE UNIQUE INDEX idx_products_channel_number ON products (channel_id, number)",
    ),
    # Back-off state for stores that block us (see tracker.check_product).
    (
        "ALTER TABLE products ADD COLUMN blocked_streak INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE products ADD COLUMN skips_left INTEGER NOT NULL DEFAULT 0",
    ),
)


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # Migrate while foreign keys are still off (SQLite's per-connection default): a table rebuild's DROP TABLE would
    # otherwise cascade-delete all price history. The pragma is a no-op inside a transaction, so order matters.
    _migrate(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, statements in enumerate(_MIGRATIONS[version:], start=version + 1):
        with conn:
            # sqlite3 only auto-begins before INSERT/UPDATE/DELETE; without this, the DDL and the version bump would
            # autocommit separately and a crash between them would re-run the step ("duplicate column").
            conn.execute("BEGIN")
            for statement in statements:
                conn.execute(statement)
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError(f"migration {number} left dangling foreign keys")
            # PRAGMA can't take bound parameters; `number` is an int we control.
            conn.execute(f"PRAGMA user_version = {number}")


def add_product(
    conn: sqlite3.Connection,
    channel_id: int,
    url: str,
    name: str | None = None,
    target_price: Decimal | None = None,
    selector: str | None = None,
    render: bool = False,
) -> Product:
    # Next number in this channel. Computed inside the INSERT, so it's atomic with it (SQLite allows one writer).
    # Numbers stay put when others are removed; only the highest is reused if it's removed and something is added.
    with conn:
        cursor = conn.execute(
            """INSERT INTO products (channel_id, number, url, name, target_price, selector, render, created_at)
               VALUES (?, (SELECT COALESCE(MAX(number), 0) + 1 FROM products WHERE channel_id = ?),
                       ?, ?, ?, ?, ?, ?)""",
            (channel_id, channel_id, url, name, _to_text(target_price), selector, render, _now()),
        )
    assert cursor.lastrowid is not None
    product = get_product(conn, cursor.lastrowid)
    assert product is not None
    return product


def remove_product(conn: sqlite3.Connection, product_id: int) -> bool:
    with conn:
        cursor = conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    return cursor.rowcount > 0


def list_products(conn: sqlite3.Connection, channel_id: int | None = None) -> list[Product]:
    """One channel's products, or every channel's when `channel_id` is None (the scheduled check)."""
    if channel_id is None:
        rows = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM products WHERE channel_id = ? ORDER BY number", (channel_id,)).fetchall()
    return [_to_product(row) for row in rows]


def get_product(conn: sqlite3.Connection, product_id: int) -> Product | None:
    """By internal id."""
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    return _to_product(row) if row else None


def find_product(conn: sqlite3.Connection, channel_id: int, number: int) -> Product | None:
    """By the number shown in a channel; another channel's products are invisible."""
    row = conn.execute("SELECT * FROM products WHERE channel_id = ? AND number = ?", (channel_id, number)).fetchone()
    return _to_product(row) if row else None


def set_target(conn: sqlite3.Connection, product_id: int, target_price: Decimal | None) -> None:
    """Also clears last_notified_price, so the next check judges the new target from scratch (a price already under
    it alerts) instead of staying quiet because an alert went out under the old target."""
    with conn:
        conn.execute(
            "UPDATE products SET target_price = ?, last_notified_price = NULL WHERE id = ?",
            (_to_text(target_price), product_id),
        )


def enable_render(conn: sqlite3.Connection, product_id: int) -> None:
    with conn:
        conn.execute("UPDATE products SET render = 1 WHERE id = ?", (product_id,))


def record_block(conn: sqlite3.Connection, product_id: int, streak: int, skips: int) -> None:
    with conn:
        conn.execute("UPDATE products SET blocked_streak = ?, skips_left = ? WHERE id = ?", (streak, skips, product_id))


def use_skip(conn: sqlite3.Connection, product_id: int) -> None:
    with conn:
        conn.execute("UPDATE products SET skips_left = skips_left - 1 WHERE id = ? AND skips_left > 0", (product_id,))


def record_price(
    conn: sqlite3.Connection,
    product: Product,
    price: Decimal,
    last_notified_price: Decimal | None,
    page_name: str | None = None,
) -> None:
    """Append to history and update the product's rolling fields in one transaction.

    `page_name` (scraped from the page) fills in `name` only when it's empty, so a name given to /track always wins.
    A successful check also clears any block back-off.
    """
    now = _now()
    lowest = price if product.lowest_price is None else min(price, product.lowest_price)
    with conn:
        conn.execute(
            "INSERT INTO price_history (product_id, price, checked_at) VALUES (?, ?, ?)",
            (product.id, str(price), now),
        )
        conn.execute(
            """UPDATE products
               SET last_price = ?, lowest_price = ?, last_notified_price = ?, last_checked_at = ?,
                   name = COALESCE(name, ?), blocked_streak = 0, skips_left = 0
               WHERE id = ?""",
            (str(price), str(lowest), _to_text(last_notified_price), now, page_name, product.id),
        )


def price_history(conn: sqlite3.Connection, product_id: int) -> list[tuple[Decimal, datetime]]:
    rows = conn.execute(
        "SELECT price, checked_at FROM price_history WHERE product_id = ? ORDER BY checked_at, id",
        (product_id,),
    ).fetchall()
    return [(Decimal(r["price"]), datetime.fromisoformat(r["checked_at"])) for r in rows]


def _to_product(row: sqlite3.Row) -> Product:
    return Product(
        id=row["id"],
        channel_id=row["channel_id"],
        number=row["number"],
        url=row["url"],
        name=row["name"],
        target_price=_to_decimal(row["target_price"]),
        selector=row["selector"],
        render=bool(row["render"]),
        last_price=_to_decimal(row["last_price"]),
        lowest_price=_to_decimal(row["lowest_price"]),
        last_notified_price=_to_decimal(row["last_notified_price"]),
        last_checked_at=datetime.fromisoformat(row["last_checked_at"]) if row["last_checked_at"] else None,
        blocked_streak=row["blocked_streak"],
        skips_left=row["skips_left"],
    )


def _to_decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _to_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _now() -> str:
    return datetime.now(UTC).isoformat()
