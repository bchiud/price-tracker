import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from price_tracker.models import PriceDrop, Product
from price_tracker.notify import ChannelNotifier, build_discord_payload


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
        "last_price": Decimal("100"),
        "lowest_price": Decimal("100"),
        "last_notified_price": None,
        "blocked_streak": 0,
        "skips_left": 0,
        "last_checked_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return Product(**fields)  # type: ignore[arg-type]


def _embed(drop: PriceDrop) -> Any:
    embeds: list[Any] = build_discord_payload(drop)["embeds"]  # type: ignore[assignment]
    assert len(embeds) == 1
    return embeds[0]


def test_payload_has_embed_with_link_and_prices() -> None:
    drop = PriceDrop(_product(), Decimal("100"), Decimal("80"), "Dropped 20.0% from 100")
    embed = _embed(drop)
    assert embed["title"] == "Price drop: Widget"
    assert embed["url"] == "https://shop/item"
    assert embed["description"] == "Dropped 20.0% from 100"
    assert [(f["name"], f["value"]) for f in embed["fields"]] == [("Now", "80.00"), ("Was", "100.00")]


def test_payload_includes_target_and_omits_missing_old_price() -> None:
    drop = PriceDrop(_product(target_price=Decimal("50")), None, Decimal("45"), "At or below your target of 50")
    embed = _embed(drop)
    assert [f["name"] for f in embed["fields"]] == ["Now", "Target"]


def test_title_truncated_to_discord_limit() -> None:
    drop = PriceDrop(_product(name="x" * 500), None, Decimal("1"), "r")
    embed = _embed(drop)
    assert len(embed["title"]) == 256


def test_title_falls_back_to_url() -> None:
    drop = PriceDrop(_product(name=None), None, Decimal("1"), "r")
    embed = _embed(drop)
    assert embed["title"] == "Price drop: https://shop/item"


class _Recorder:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status)


def test_payload_never_pings() -> None:
    drop = PriceDrop(_product(name="@everyone free stuff"), None, Decimal("1"), "r")
    assert build_discord_payload(drop)["allowed_mentions"] == {"parse": []}


def test_alert_goes_to_the_products_channel_as_bot() -> None:
    recorder = _Recorder()
    with httpx.Client(transport=httpx.MockTransport(recorder)) as client:
        ChannelNotifier(client, "BOTTOKEN")(PriceDrop(_product(channel_id=555), None, Decimal("80"), "r"))

    [request] = recorder.requests
    assert (request.method, str(request.url)) == ("POST", "https://discord.com/api/v10/channels/555/messages")
    assert request.headers["Authorization"] == "Bot BOTTOKEN"
    assert request.headers["User-Agent"].startswith("DiscordBot (")
    assert json.loads(request.content)["embeds"][0]["fields"][0]["value"] == "80.00"


def test_alert_raises_on_http_error() -> None:
    with (
        httpx.Client(transport=httpx.MockTransport(_Recorder(status=403))) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        ChannelNotifier(client, "BOTTOKEN")(PriceDrop(_product(), None, Decimal("1"), "r"))
