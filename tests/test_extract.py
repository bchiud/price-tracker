import json
from decimal import Decimal

import pytest

from price_tracker.extract import extract_name, extract_price, parse_price


def _json_ld(data: object) -> str:
    return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head></html>'


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("19.99", Decimal("19.99")),
        ("$1,299.99", Decimal("1299.99")),
        ("1.299,99 €", Decimal("1299.99")),
        ("19,99", Decimal("19.99")),
        ("1,299", Decimal("1299")),
        ("1,299,999", Decimal("1299999")),
        ("1.299.999", Decimal("1299999")),
        ("USD 45.", Decimal("45")),
        (42, Decimal("42")),
        (19.5, Decimal("19.5")),
        ("Price: £7.00 each", Decimal("7.00")),
    ],
)
def test_parse_price(raw: object, expected: Decimal) -> None:
    assert parse_price(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "free", "0", "0.00", True, {"price": 1}])
def test_parse_price_rejects(raw: object) -> None:
    assert parse_price(raw) is None


def test_json_ld_offer() -> None:
    html = _json_ld({"@type": "Product", "offers": {"@type": "Offer", "price": "49.99"}})
    assert extract_price(html) == Decimal("49.99")


def test_json_ld_numeric_price() -> None:
    assert extract_price(_json_ld({"@type": "Product", "offers": {"price": 12}})) == Decimal("12")


def test_json_ld_aggregate_offer_uses_low_price() -> None:
    html = _json_ld({"@type": "Product", "offers": {"@type": "AggregateOffer", "lowPrice": "10", "highPrice": "20"}})
    assert extract_price(html) == Decimal("10")


def test_json_ld_offer_list_takes_lowest() -> None:
    html = _json_ld({"@type": "Product", "offers": [{"price": "30"}, {"price": "25"}, {"price": "n/a"}]})
    assert extract_price(html) == Decimal("25")


def test_json_ld_price_specification() -> None:
    html = _json_ld({"@type": "Product", "offers": {"priceSpecification": {"price": "8.50"}}})
    assert extract_price(html) == Decimal("8.50")


def test_json_ld_inside_graph_with_type_list() -> None:
    html = _json_ld(
        {
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "WebPage", "name": "x"},
                {"@type": ["Product", "Thing"], "offers": {"price": "5.25"}},
            ],
        }
    )
    assert extract_price(html) == Decimal("5.25")


def test_json_ld_product_group_variant() -> None:
    html = _json_ld({"@type": "ProductGroup", "hasVariant": [{"@type": "Product", "offers": {"price": "77"}}]})
    assert extract_price(html) == Decimal("77")


def test_skips_invalid_and_non_product_json_ld() -> None:
    html = (
        '<script type="application/ld+json">{not json</script>'
        '<script type="application/ld+json">{"@type": "Organization", "name": "Shop"}</script>'
        '<script type="application/ld+json">{"@type": "Product", "offers": {"price": "3"}}</script>'
    )
    assert extract_price(html) == Decimal("3")


def test_ignores_price_outside_product() -> None:
    # A bare Offer with no Product wrapper isn't trusted.
    assert extract_price(_json_ld({"@type": "Offer", "price": "9"})) is None


@pytest.mark.parametrize(
    "tag",
    [
        '<meta property="product:price:amount" content="15.00">',
        '<meta property="og:price:amount" content="15.00">',
        '<meta itemprop="price" content="15.00">',
        '<span itemprop="price" content="15.00">$15</span>',
    ],
)
def test_meta_tags(tag: str) -> None:
    assert extract_price(f"<html><head>{tag}</head></html>") == Decimal("15.00")


def test_json_ld_wins_over_meta() -> None:
    html = _json_ld({"@type": "Product", "offers": {"price": "1"}}).replace(
        "</head>", '<meta property="og:price:amount" content="2"></head>'
    )
    assert extract_price(html) == Decimal("1")


def test_selector_wins_over_json_ld() -> None:
    # e.g. a discount app rewrites the visible price but leaves the structured data at full price.
    html = _json_ld({"@type": "Product", "offers": {"price": "160.00"}}).replace(
        "</head>", '</head><body><span class="sale">96,00 €</span></body>'
    )
    assert extract_price(html) == Decimal("160.00")
    assert extract_price(html, ".sale") == Decimal("96.00")


def test_missing_selector_falls_back_to_json_ld() -> None:
    html = _json_ld({"@type": "Product", "offers": {"price": "160.00"}})
    assert extract_price(html, ".sale") == Decimal("160.00")


def test_selector_text() -> None:
    html = '<div class="price-box"><span class="now">$1,049.00</span></div>'
    assert extract_price(html, ".price-box .now") == Decimal("1049.00")


def test_selector_prefers_content_attribute() -> None:
    assert extract_price('<span id="p" content="9.99">Sale!</span>', "#p") == Decimal("9.99")


def test_selector_missing_node() -> None:
    assert extract_price("<div></div>", ".nope") is None


def test_no_price() -> None:
    assert extract_price("<html><body>Out of stock</body></html>") is None


def test_name_from_json_ld_product() -> None:
    html = _json_ld({"@type": "Product", "name": "Silk &amp; Lace  Widget", "offers": {"price": "1"}}).replace(
        "</head>", "<title>Ignored | Shop</title></head>"
    )
    assert extract_name(html) == "Silk & Lace Widget"


def test_name_falls_back_to_og_title_then_title() -> None:
    og = '<html><head><meta property="og:title" content="OG Name"><title>Title | Shop</title></head></html>'
    assert extract_name(og) == "OG Name"
    assert extract_name("<html><head><title>\n  Title | Shop \n</title></head></html>") == "Title | Shop"


@pytest.mark.parametrize("html", ["<html></html>", "<title>   </title>", _json_ld({"@type": "Product", "name": ""})])
def test_no_name(html: str) -> None:
    assert extract_name(html) is None


def test_embedded_state_price_as_last_resort() -> None:
    # Williams Sonoma: no Product JSON-LD or meta tags; the price only lives in the app's embedded JSON.
    html = (
        '<script type="application/ld+json">{"@type": "FAQPage"}</script>'
        '<script>window.__STATE__ = {"loyaltyRewardPriceTiers": {"lowPrice": [125, 749.99]}, '
        '"name": "Made In Stainless-Steel Saucier, 3-Qt", '
        '"price": {"regularPrice": 179.95, "retailPrice": 179.95, "sellingPrice": 179.95}}</script>'
    )
    assert extract_price(html) == Decimal("179.95")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ('"salePrice": null, "sellingPrice": "$24.50"', Decimal("24.50")),  # skips unusable values
        ('"salePrice": 0, "currentPrice": 12', Decimal("12")),
        ('"salePrice": {"value": 5}', None),  # nested objects aren't guessed at
        ('"price": 99', None),  # bare "price" is too generic to trust here
    ],
)
def test_embedded_state_values(state: str, expected: Decimal | None) -> None:
    assert extract_price(f"<script>var s = {{{state}}};</script>") == expected


def test_structured_data_wins_over_embedded_state() -> None:
    html = _json_ld({"@type": "Product", "offers": {"price": "50"}}) + '<script>{"sellingPrice": 10}</script>'
    assert extract_price(html) == Decimal("50")
