from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlsplit


def format_price(value: Decimal) -> str:
    """Display form, always two decimals: 120.0 -> "120.00". Stored values keep whatever precision the page gave."""
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


# Under a two-letter country code these form one suffix: "co.uk", "com.au", "org.nz", ...
_SECOND_LEVEL_SUFFIXES = {"co", "com", "net", "org", "gov", "edu", "ac"}


@dataclass(frozen=True)
class Product:
    id: int
    # Discord channel the product was tracked from; its alerts go there.
    channel_id: int
    # The ID shown in that channel (1, 2, 3, ...); `id` is internal and global across channels.
    number: int
    url: str
    name: str | None
    target_price: Decimal | None
    # Optional CSS selector for the price; takes precedence over structured data when set.
    selector: str | None
    # Load the page in headless Chromium so prices drawn by JavaScript are visible.
    render: bool
    last_price: Decimal | None
    lowest_price: Decimal | None
    # Price at the most recent alert; suppresses repeat alerts while the price stays put.
    last_notified_price: Decimal | None
    last_checked_at: datetime | None
    # Back-off after the store blocks us: consecutive blocks, and checks left to skip before trying again.
    blocked_streak: int
    skips_left: int

    @property
    def label(self) -> str:
        return self.name or self.url

    @property
    def store(self) -> str:
        """The site's name from its domain: https://www.williams-sonoma.com/... -> "Williams-Sonoma"."""
        labels = (urlsplit(self.url).hostname or self.url).split(".")
        # Drop the public suffix: ".com", or a two-part one like ".co.uk". A heuristic rather than the full
        # Public Suffix List, which is plenty for a display label.
        if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL_SUFFIXES:
            labels = labels[:-2]
        elif len(labels) >= 2:
            labels = labels[:-1]
        # The registered name is the last label left; any subdomain ("www", "shop") comes before it.
        return labels[-1].title()


@dataclass(frozen=True)
class PriceDrop:
    product: Product
    old_price: Decimal | None
    new_price: Decimal
    reason: str


class HTTPStatusError(RuntimeError):
    """A page fetch got an error status. `status` lets callers tell a block (403/429) from other failures."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


@dataclass(frozen=True)
class CartPrice:
    """A Shopify cart's price for one unit, after automatic discounts, and those discounts' titles."""

    price: Decimal
    discounts: tuple[str, ...]
