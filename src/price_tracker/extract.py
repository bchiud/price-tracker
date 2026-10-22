"""Pull a product's price and name out of a page's HTML.

Price sources, tried in order:
1. A caller-supplied CSS selector. It wins because it's explicit, and because structured data is stale on pages
   where JavaScript rewrites the visible price (e.g. a discount app).
2. JSON-LD schema.org `Product` -> `offers.price` (or `AggregateOffer.lowPrice`)
3. Price meta tags (`product:price:amount`, `og:price:amount`, `itemprop="price"`)
4. Last resort: the first well-known price key (`sellingPrice`, `salePrice`, ...) in the page's embedded JSON state,
   for sites that only put the price in their JavaScript app data (e.g. Williams Sonoma). The first match is usually
   the page's own product, but on some sites it could be a related one, hence last.

Name sources, tried in order: JSON-LD `Product.name`, the `og:title` meta tag, then `<title>`.
"""

import json
import re
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from html import unescape
from typing import Any

from selectolax.lexbor import LexborHTMLParser

_META_SELECTORS = (
    'meta[property="product:price:amount"]',
    'meta[property="og:price:amount"]',
    'meta[itemprop="price"]',
    '[itemprop="price"][content]',
)

# "sellingPrice": 179.95  /  "salePrice":"$19.99"; a nested object (e.g. "salePrice": {...}) doesn't match.
_EMBEDDED_PRICE = re.compile(r'"(?:sellingPrice|salePrice|currentPrice|finalPrice)"\s*:\s*"?([^",}\]]{1,20})')


def extract_price(html: str, selector: str | None = None) -> Decimal | None:
    tree = LexborHTMLParser(html)
    return _from_selector(tree, selector) or _from_json_ld(tree) or _from_meta(tree) or _from_embedded_state(html)


def extract_name(html: str) -> str | None:
    tree = LexborHTMLParser(html)
    for product in _json_ld_products(tree):
        if name := _clean(product.get("name")):
            return name
    og_title = tree.css_first('meta[property="og:title"]')
    if og_title is not None and (name := _clean(og_title.attributes.get("content"))):
        return name
    title = tree.css_first("title")
    return _clean(title.text()) if title is not None else None


def _clean(raw: object) -> str | None:
    """Unescape entities (JSON-LD often carries "&amp;") and collapse whitespace; None if nothing's left."""
    if not isinstance(raw, str):
        return None
    return " ".join(unescape(raw).split()) or None


def parse_price(raw: object) -> Decimal | None:
    """Parse "$1,299.99", "1.299,99 €", 42, etc. into a Decimal; None if unparseable or non-positive."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        raw = str(raw)
    if not isinstance(raw, str):
        return None

    match = re.search(r"\d[\d.,]*", raw)
    if not match:
        return None
    number = match.group().rstrip(".,")

    if "," in number and "." in number:
        # Whichever separator comes last is the decimal point.
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        # A lone comma followed by exactly 2 digits is a decimal comma ("19,99"); otherwise thousands.
        whole, _, frac = number.rpartition(",")
        number = f"{whole.replace(',', '')}.{frac}" if len(frac) == 2 else number.replace(",", "")
    elif number.count(".") > 1:
        # "1.299.999" -> thousands separators only.
        number = number.replace(".", "")

    try:
        price = Decimal(number)
    except InvalidOperation:
        return None
    return price if price > 0 else None


def _from_json_ld(tree: LexborHTMLParser) -> Decimal | None:
    for product in _json_ld_products(tree):
        price = _price_from_offers(product.get("offers"))
        if price is not None:
            return price
    return None


def _json_ld_products(tree: LexborHTMLParser) -> Iterator[dict[str, Any]]:
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text())
        except ValueError:
            continue
        yield from _iter_products(data)


def _iter_products(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every schema.org Product object, wherever it's nested (lists, @graph, etc.)."""
    if isinstance(node, list):
        for item in node:
            yield from _iter_products(item)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" in types or "ProductGroup" in types:
            yield node
        for value in node.values():
            if isinstance(value, dict | list):
                yield from _iter_products(value)


def _price_from_offers(offers: Any) -> Decimal | None:
    if isinstance(offers, list):
        prices = [p for o in offers if (p := _price_from_offers(o)) is not None]
        return min(prices, default=None)
    if not isinstance(offers, dict):
        return None
    for key in ("price", "lowPrice"):
        price = parse_price(offers.get(key))
        if price is not None:
            return price
    # Some sites nest the amount in priceSpecification.
    return _price_from_offers(offers.get("priceSpecification"))


def _from_meta(tree: LexborHTMLParser) -> Decimal | None:
    for css in _META_SELECTORS:
        node = tree.css_first(css)
        if node is not None:
            price = parse_price(node.attributes.get("content"))
            if price is not None:
                return price
    return None


def _from_selector(tree: LexborHTMLParser, selector: str | None) -> Decimal | None:
    if not selector:
        return None
    node = tree.css_first(selector)
    if node is None:
        return None
    return parse_price(node.attributes.get("content") or node.text())


def _from_embedded_state(html: str) -> Decimal | None:
    for match in _EMBEDDED_PRICE.finditer(html):
        price = parse_price(match.group(1))
        if price is not None:
            return price
    return None
