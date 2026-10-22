"""What you'd actually pay on a Shopify store: the product's price after the store's automatic discounts.

A "15% Off Sitewide" automatic discount isn't in the product page's price data; Shopify applies it in the cart (the
theme may then display it on the page with JavaScript). So for a Shopify product page we add the variant to a fresh,
anonymous cart and read the cart's final line price. Discount codes aren't involved: only automatic discounts apply.
"""

import json
import re
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

from curl_cffi import requests as curl_requests

from price_tracker.models import CartPrice

# /products/<handle>, optionally under a locale prefix like /en/
_PRODUCT_PATH = re.compile(r"^(.*?/products/[^/?#]+)")


def is_product_page(url: str, html: str) -> bool:
    return _PRODUCT_PATH.match(urlsplit(url).path) is not None and ("cdn.shopify.com" in html or "Shopify.shop" in html)


def cart_price(session: curl_requests.Session[curl_requests.Response], url: str, timeout: float) -> CartPrice | None:
    """Price of one unit of the URL's variant (`?variant=`, else the first available) in `session`'s cart.
    `session` must start with an empty cart (fresh cookies). None if the store won't price it (e.g. sold out)."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    match = _PRODUCT_PATH.match(parts.path)
    if match is None:
        return None

    variant_id = _variant_from_query(parts.query)
    if variant_id is None:
        product = session.get(f"{origin}{match.group(1)}.js", timeout=timeout)
        if product.status_code != 200:
            return None
        variant_id = _default_variant(json.loads(product.text))
        if variant_id is None:
            return None

    added = session.post(f"{origin}/cart/add.js", json={"items": [{"id": variant_id, "quantity": 1}]}, timeout=timeout)
    if added.status_code != 200:  # 422 when sold out or the variant doesn't exist
        return None
    cart = session.get(f"{origin}/cart.js", timeout=timeout)
    if cart.status_code != 200:
        return None
    return parse_cart(json.loads(cart.text))


def parse_cart(cart: dict[str, Any]) -> CartPrice | None:
    items = cart.get("items") or []
    if len(items) != 1 or not items[0].get("quantity"):
        return None
    item = items[0]
    # Shopify amounts are integers in the currency's minor unit (cents).
    price = Decimal(item["final_line_price"]) / item["quantity"] / 100
    titles = [
        allocation.get("discount_application", {}).get("title")
        for allocation in item.get("line_level_discount_allocations", [])
    ]
    titles += [application.get("title") for application in cart.get("cart_level_discount_applications", [])]
    return CartPrice(price, tuple(t for t in titles if t))


def _variant_from_query(query: str) -> int | None:
    values = parse_qs(query).get("variant")
    return int(values[0]) if values and values[0].isdigit() else None


def _default_variant(product: dict[str, Any]) -> int | None:
    variants = product.get("variants") or []
    chosen = next((v for v in variants if v.get("available")), variants[0] if variants else None)
    return int(chosen["id"]) if chosen else None
