import httpx

from price_tracker.models import PriceDrop, format_price

_GREEN = 0x2ECC71
_DISCORD_API = "https://discord.com/api/v10"


def build_discord_payload(drop: PriceDrop) -> dict[str, object]:
    product = drop.product
    fields = [
        {"name": "Now", "value": format_price(drop.new_price), "inline": True},
    ]
    if drop.old_price is not None:
        fields.append({"name": "Was", "value": format_price(drop.old_price), "inline": True})
    if product.target_price is not None:
        fields.append({"name": "Target", "value": format_price(product.target_price), "inline": True})

    return {
        # Product names come from users/pages; never let one ping @everyone or a role.
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": f"Price drop: {product.label}"[:256],  # Discord's embed title limit
                "url": product.url,
                "description": drop.reason,
                "color": _GREEN,
                "fields": fields,
            }
        ],
    }


class ChannelNotifier:
    """Posts each alert as the bot to the channel its product was tracked from (Discord REST API, bot token auth)."""

    def __init__(self, client: httpx.Client, bot_token: str) -> None:
        self._client = client
        self._headers = {
            "Authorization": f"Bot {bot_token}",
            # Discord requires this format for bots; a browser User-Agent gets 403 "internal network error" (40333).
            "User-Agent": "DiscordBot (price-tracker, 0.1.0)",
        }

    def __call__(self, drop: PriceDrop) -> None:
        response = self._client.post(
            f"{_DISCORD_API}/channels/{drop.product.channel_id}/messages",
            json=build_discord_payload(drop),
            headers=self._headers,
        )
        # e.g. 403 if the bot can't post there; check_product then skips recording so the alert is retried.
        response.raise_for_status()
