"""Shopify cart pricing. The end-to-end tests run PageFetcher.cart_price against a fake Shopify store over real HTTP."""

import json
import threading
from collections.abc import Iterator
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count

import pytest

from price_tracker import shopify
from price_tracker.fetch import PageFetcher
from price_tracker.models import CartPrice

SHOPIFY_PAGE = '<html><script src="https://cdn.shopify.com/s/files/theme.js"></script></html>'


@pytest.mark.parametrize(
    ("url", "html", "expected"),
    [
        ("https://shop.com/products/widget?variant=1", SHOPIFY_PAGE, True),
        ("https://shop.com/en/products/widget", "<script>Shopify.shop = 'x';</script>", True),  # locale prefix
        ("https://shop.com/collections/all", SHOPIFY_PAGE, False),  # not a product page
        ("https://shop.com/products/widget", "<html>no shopify here</html>", False),
    ],
)
def test_is_product_page(url: str, html: str, expected: bool) -> None:
    assert shopify.is_product_page(url, html) is expected


def test_parse_cart_reads_final_price_and_discount_titles() -> None:
    cart = {
        "items": [
            {
                "quantity": 1,
                "original_line_price": 12000,
                "final_line_price": 10200,
                "line_level_discount_allocations": [{"discount_application": {"title": "15% Off Sitewide"}}],
            }
        ],
        "cart_level_discount_applications": [{"title": "Free Gift"}],
    }
    assert shopify.parse_cart(cart) == CartPrice(Decimal("102"), ("15% Off Sitewide", "Free Gift"))


def test_parse_cart_is_per_unit() -> None:
    cart = {"items": [{"quantity": 2, "final_line_price": 1999}]}
    assert shopify.parse_cart(cart) == CartPrice(Decimal("9.995"), ())


@pytest.mark.parametrize(
    "cart",
    [
        {},
        {"items": []},
        {"items": [{"quantity": 1, "final_line_price": 100}, {"quantity": 1, "final_line_price": 100}]},
    ],
)
def test_parse_cart_needs_exactly_one_item(cart: dict[str, object]) -> None:
    assert shopify.parse_cart(cart) is None


class FakeShopify:
    """Just enough of a Shopify store: a product page, /products/<handle>.js, and a cookie-keyed cart."""

    def __init__(self) -> None:
        self.carts: dict[str, list[int]] = {}
        self.ids = count(1)
        self.variants = [
            {"id": 11, "price": 12000, "available": False},  # sold out
            {"id": 12, "price": 15000, "available": True},
        ]
        self.discount_percent = 15

    def price_of(self, variant_id: int) -> int:
        return next(int(v["price"]) for v in self.variants if v["id"] == variant_id)

    def handler(self) -> type[BaseHTTPRequestHandler]:
        store = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def _send(self, status: int, body: object, cookie: str | None = None) -> None:
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                if cookie:
                    self.send_header("Set-Cookie", f"cart={cookie}; Path=/")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _cart_id(self) -> str | None:
                for part in (self.headers.get("Cookie") or "").split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == "cart":
                        return value
                return None

            def do_GET(self) -> None:
                if self.path.startswith("/products/widget.js"):
                    self._send(200, {"variants": store.variants})
                elif self.path.startswith("/cart.js"):
                    items = store.carts.get(self._cart_id() or "", [])
                    lines = []
                    for vid in items:
                        original = store.price_of(vid)
                        discount = original * store.discount_percent // 100
                        allocations = [{"discount_application": {"title": f"{store.discount_percent}% Off"}}]
                        lines.append(
                            {
                                "quantity": 1,
                                "final_line_price": original - discount,
                                "line_level_discount_allocations": allocations if discount else [],
                            }
                        )
                    self._send(200, {"items": lines})
                else:
                    self._send(200, SHOPIFY_PAGE)

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                vid = body["items"][0]["id"]
                variant = next((v for v in store.variants if v["id"] == vid), None)
                if variant is None or not variant["available"]:
                    self._send(422, {"description": "sold out"})
                    return
                cart_id = self._cart_id() or str(next(store.ids))
                store.carts.setdefault(cart_id, []).append(vid)
                self._send(200, {}, cookie=cart_id)

        return Handler


@pytest.fixture
def store() -> Iterator[tuple[FakeShopify, str]]:
    fake = FakeShopify()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield fake, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_cart_price_end_to_end(store: tuple[FakeShopify, str]) -> None:
    fake, base = store
    with PageFetcher(timeout_seconds=5) as fetch:
        url = f"{base}/products/widget?variant=12"
        first = fetch.cart_price(url, fetch(url))
        second = fetch.cart_price(url, fetch(url))
    assert first == CartPrice(Decimal("127.5"), ("15% Off",))
    # A fresh cart each time: a reused session would hold two items, and parse_cart would refuse it.
    assert second == first
    assert sorted(len(items) for items in fake.carts.values()) == [1, 1]


def test_cart_price_picks_first_available_variant_without_query(store: tuple[FakeShopify, str]) -> None:
    _, base = store
    with PageFetcher(timeout_seconds=5) as fetch:
        cart = fetch.cart_price(f"{base}/products/widget", SHOPIFY_PAGE)
    assert cart is not None and cart.price == Decimal("127.5")  # variant 12; 11 is sold out


def test_sold_out_variant_gives_no_cart_price(store: tuple[FakeShopify, str]) -> None:
    _, base = store
    with PageFetcher(timeout_seconds=5) as fetch:
        assert fetch.cart_price(f"{base}/products/widget?variant=11", SHOPIFY_PAGE) is None


def test_non_shopify_page_makes_no_cart_requests(store: tuple[FakeShopify, str]) -> None:
    fake, base = store
    with PageFetcher(timeout_seconds=5) as fetch:
        assert fetch.cart_price(f"{base}/products/widget", "<html>plain store</html>") is None
    assert fake.carts == {}
