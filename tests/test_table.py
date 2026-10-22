from datetime import UTC, datetime
from decimal import Decimal

import pytest

from price_tracker.models import Product, format_price
from price_tracker.table import format_table, product_table


def _product(**overrides: object) -> Product:
    fields: dict[str, object] = {
        "id": 1,
        "channel_id": 1,
        "number": 1,
        "url": "https://shop/item",
        "name": "Widget",
        "target_price": None,
        "selector": None,
        "render": False,
        "last_price": Decimal("96.00"),
        "lowest_price": Decimal("80.00"),
        "last_notified_price": None,
        "blocked_streak": 0,
        "skips_left": 0,
        "last_checked_at": datetime(2026, 10, 22, 12, 30, tzinfo=UTC),
    }
    fields.update(overrides)
    return Product(**fields)  # type: ignore[arg-type]


def test_format_table_aligns_columns() -> None:
    lines = format_table(["ID", "Name", "Price"], [["1", "A", "9.99"], ["12", "Longer", "100"]], right_aligned={0, 2})
    assert lines == [
        "ID  Name    Price",
        "--  ------  -----",
        " 1  A        9.99",
        "12  Longer    100",
    ]


def test_product_table_columns_and_placeholders() -> None:
    lines = product_table(
        [
            _product(),
            _product(
                id=2, name=None, last_price=None, lowest_price=None, last_checked_at=None, target_price=Decimal("50")
            ),
        ]
    )
    assert lines[0].split() == ["ID", "Store", "Product", "Now", "Low", "Target", "Checked"]
    assert lines[2].split()[:6] == ["1", "Shop", "Widget", "96.00", "80.00", "-"]
    assert lines[3].split() == ["1", "Shop", "https://shop/item", "-", "-", "50.00", "-"]


def test_long_names_are_truncated() -> None:
    [_, _, row] = product_table([_product(name="Extra-long product name in Blue, size 46")])
    assert "Extra-long product name…" in row
    assert "size 46" not in row


def test_id_column_is_the_channel_number() -> None:
    [_, _, row] = product_table([_product(id=99, number=3)])
    assert row.split()[0] == "3"


@pytest.mark.parametrize(
    ("url", "store"),
    [
        ("https://www.acme.com/s/deluxe-widget/123456?color=x", "Acme"),
        ("https://www.williams-sonoma.com/products/x/", "Williams-Sonoma"),
        ("https://WWW.Globex.FR/en/products/x", "Globex"),
        ("https://initech.com/collections/x", "Initech"),
        ("https://shop.example.co.uk/p/1", "Example"),  # two-part suffix and a subdomain
        ("https://store.example.com.au/p/1", "Example"),
        ("https://example.co/p/1", "Example"),  # ".co" alone is the whole suffix
        ("https://example.eu/p/1", "Example"),
        ("http://localhost:8000/p", "Localhost"),
    ],
)
def test_store_is_the_title_cased_site_name(url: str, store: str) -> None:
    assert _product(url=url).store == store


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("120.0", "120.00"),
        ("198", "198.00"),
        ("19.9", "19.90"),
        ("96.00", "96.00"),
        ("9.995", "10.00"),
        ("0.125", "0.13"),
    ],
)
def test_format_price_always_two_decimals(value: str, shown: str) -> None:
    assert format_price(Decimal(value)) == shown
