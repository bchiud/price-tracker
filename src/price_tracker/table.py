"""Plain-text tables for Discord code blocks (Discord doesn't render Markdown tables)."""

from collections.abc import Collection, Sequence
from decimal import Decimal

from price_tracker.models import Product, format_price

# Keep the table narrow enough not to wrap in a typical Discord window.
_STORE_WIDTH = 18
_NAME_WIDTH = 24
_EMPTY = "-"


def format_table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], right_aligned: Collection[int] = ()
) -> list[str]:
    """Return the table as lines: header, separator, then one line per row."""
    widths = [max([len(h), *(len(row[i]) for row in rows)]) for i, h in enumerate(headers)]

    def line(cells: Sequence[str]) -> str:
        padded = (
            c.rjust(w) if i in right_aligned else c.ljust(w) for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        )
        return "  ".join(padded).rstrip()

    return [line(headers), line(["-" * w for w in widths]), *(line(row) for row in rows)]


def product_table(products: Sequence[Product]) -> list[str]:
    headers = ["ID", "Store", "Product", "Now", "Low", "Target", "Checked"]
    rows = [
        [
            str(p.number),
            _truncate(p.store, _STORE_WIDTH),
            _truncate(p.label, _NAME_WIDTH),
            _price(p.last_price),
            _price(p.lowest_price),
            _price(p.target_price),
            f"{p.last_checked_at.astimezone():%m-%d %H:%M}" if p.last_checked_at else _EMPTY,
        ]
        for p in products
    ]
    return format_table(headers, rows, right_aligned={0, 3, 4, 5})


def _price(value: Decimal | None) -> str:
    return format_price(value) if value is not None else _EMPTY


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"
