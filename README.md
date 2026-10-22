# price-tracker

A Discord bot that tracks product prices and posts an alert when they drop. You manage everything with slash commands. Each channel has its own separate list of tracked products.

## Quick start

```sh
uv sync
uv run playwright install chromium   # one-time ~100 MB download; used by products tracked with render
cp .env.example .env                 # fill in DISCORD_BOT_TOKEN and DISCORD_GUILD_ID (see Discord setup)
uv run price-tracker                 # runs the bot: slash commands + a check every CHECK_INTERVAL_HOURS (default 12)
```

The bot must keep running for commands and checks to work. If your Mac sleeps, the bot goes offline.

## Slash commands

Every command applies only to **the channel it's used in**:
- A channel sees, checks and removes only its own products.
- Price-drop alerts are posted back to the channel the product was tracked from.
- The same URL can be tracked in two channels with different targets.

All commands are **owner-only**: only the account that owns the bot application can use them.

| Command | What it does |
|---|---|
| `/track url [name] [target] [selector] [render]` | Start tracking; checks right away and replies with the current price |
| `/list` | This channel's products as a table (ID, store, name, now, low, target, last checked) |
| `/history id` | Store and product link (`Acme: Deluxe Widget`), then the last 20 prices |
| `/target id [price]` | Set the target: an amount (`150`) or a percentage below the current price (`-20%`). Leave `price` empty to clear it |
| `/untrack id` | Stop tracking (also deletes its history) |
| `/check` | Check this channel's products now |

**IDs are per channel:** each channel numbers its products 1, 2, 3… in the order they were tracked. A product keeps its number when others are removed, so `/untrack 2` never hits a product that moved. (Only the highest number can be reused, after it's removed and something new is tracked.)

**Store** is the site's name from the URL's domain, without `www.` and the ending (`.com`, `.co.uk`, …), in title case. For example, `www.williams-sonoma.com` shows as `Williams-Sonoma`.

Links can't be clicked inside `/list`'s table, because Discord only aligns columns inside a code block. `/history` starts with the product's clickable link.

**Names:** if you don't give one, the product name is read from the page: the structured data's `Product.name`, then `og:title`, then `<title>`. It's only taken from a page that also gave a price, so a bot-check page's title never becomes the name. A `name` you give is never overwritten.

## Discord setup

1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. Open the **Bot** tab, click **Reset Token**, and copy the token into `.env` as `DISCORD_BOT_TOKEN`.
   - Leave the privileged intents off; the bot doesn't need them.
3. Open **OAuth2 → URL Generator**, tick the scopes `bot` and `applications.commands`, open the generated URL, and add the bot to your server.
   - The bot posts alerts with its own permissions, so it needs **Send Messages** and **Embed Links** in the channels you use. Default server roles usually allow both.
4. Turn on Developer Mode in Discord (**User Settings → Advanced**). Right-click your server, choose **Copy Server ID**, and put it in `.env` as `DISCORD_GUILD_ID`.
   - Without it, commands sync globally and can take a while to show up.

## Configuration

Settings come from environment variables or `.env` in the working directory.

| Variable | Default | Notes |
|---|---|---|
| `DISCORD_BOT_TOKEN` | unset | Required |
| `DISCORD_GUILD_ID` | unset | Makes slash commands appear instantly |
| `CHECK_INTERVAL_HOURS` | `12` | Must be > 0; decimals allowed (`0.5` = 30 min) |
| `DB_PATH` | `price_tracker.db` | Relative to the working directory |
| `REQUEST_TIMEOUT_SECONDS` | `20` | Per-page fetch timeout (and the render wait) |

## How it works

**Reading the price.** Sources are tried in this order:
1. Your `selector`, if set. It comes first because it's explicit, and because the structured data is stale on pages where JavaScript rewrites the price.
2. JSON-LD schema.org `Product` → `offers.price` (or `AggregateOffer.lowPrice`)
3. Price `<meta>` tags
4. Last resort: the first well-known price key (`sellingPrice`, `salePrice`, `currentPrice`, `finalPrice`) in the page's embedded app data. This is for sites like Williams Sonoma that only keep the price in their JavaScript app data. It's last because on some sites the first match could be a related product.

**Shopify automatic discounts.** A store-wide "15% Off Sitewide" discount isn't part of a product's listed price; Shopify applies it in the cart. For Shopify product pages, each check adds the URL's variant (`?variant=`, otherwise the first one in stock) to a fresh, anonymous cart and reads the price there. That's two or three quick requests, with no browser.
- **Without a selector:** the cart price is used. It's what you'd pay for that exact variant.
- **With a selector:** the lower of the two is used. A selector can point at a sale price that a script draws outside Shopify's discounts, which the cart doesn't know about.
- **Alerts name the discount,** e.g. "Dropped 15.0% from 120.00 (15% Off Sitewide)".
- **Discount codes aren't applied,** and a sold-out variant or any cart error just keeps the page price.

**Fetching.** Plain fetches imitate Chrome down to the TLS handshake (`curl_cffi`). Bot protection like Akamai blocks clients by that handshake, so a normal Python HTTP client gets `403` even with a Chrome User-Agent.

To build a selector: in your browser, right-click the price, choose **Inspect**, and write a CSS selector for that element.

**Headless browser (`render`).** Some pages need a real browser:
- **Bot checks:** e.g. some stores serve a JavaScript challenge instead of the product.
- **Prices drawn by scripts:** e.g. a Shopify discount app showing a sale price.

**Automatic:** if a plain fetch errors or finds no price, the check retries once in headless Chromium. If that works, the product is switched to `render` for good, and later checks skip the plain fetch. `/track` says when this happens.

**Manual:** the automatic retry can't catch a page that has a price, just the *wrong* one. For example, a discount app might show the sale price while the page's data still has the full price. For those:
- Track the product with `render: True` and a `selector` for the sale price.
- The page is then loaded in headless Chromium, and the tracker waits up to `REQUEST_TIMEOUT_SECONDS` for the selector to appear.
- If it never appears (e.g. the sale ended and the app stopped adding its element), the tracker falls back to the page as rendered, which usually gives the regular price.
- Rendered checks take a few seconds each. Chromium starts only when a check includes a rendered product, and closes after the check.

Example: a Shopify store whose Sales Genius discount app draws the sale price:
```
/track url:https://shop.example.com/products/<product> render:True
       selector:#price-sg-fetch-main-product-price .t-text--bold
```

**Targets.** `/target id:3 price:-20%` takes 20% off the current price, rounded to the cent, e.g. 96.00 becomes 76.80. Only negative percentages are accepted, since `20%` could mean either direction. Percentages need a current price, so `/track` only takes an amount; use `/target` once the product has been checked. Changing a target resets the alert state, so the next check judges the new target from scratch. If the price is already at or below it, you get an alert.

**When it alerts:**
- **With `target`:** when the price is at or below the target. It alerts again only if the price goes lower, or rises above the target and comes back down.
- **Without a target:** when the price drops below the previous check. The first check only records a baseline. It never alerts twice for the same or a higher price, and it re-arms once the price climbs back above the last alerted price.
- **If an alert fails to post** (Discord down, or the bot can't post in the channel), the price isn't recorded, so the next check retries the alert.

**Avoiding bot blocks.** There's no guarantee, but the tracker keeps a low profile:
- **Low volume:** one request per product per check, one at a time.
- **Chrome fingerprint:** plain fetches imitate Chrome, and the browser is only used when needed.
- **Spacing:** within a check, requests to the same store are 3–8 seconds apart (random). Different stores aren't delayed.
- **Back-off:** if a store blocks a product (HTTP 403 or 429, even after the browser fallback), it's skipped for 1 check, then 2, 4, and at most 8. `/check` shows these as ⏸️. The first successful check resets it.
- **Jitter:** each scheduled check is set ±10% around `CHECK_INTERVAL_HOURS`, so checks don't land at the same time of day.
- **Home connection:** running it on a home connection helps; sites trust home IP addresses more than cloud servers.

**Limits:**
- Some big retailers (Amazon, Walmart) block scrapers and forbid scraping in their terms of service.
- **Region:** prices can differ by country (VAT, regional pricing). The tracker sees whatever the store serves to this machine with a fresh browser session, which may not match a browser that has a country saved.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

## Layout

| Module | Responsibility |
|---|---|
| `cli.py` | Entry point: checks config, runs migrations, starts the bot |
| `bot.py` | discord.py slash commands and the scheduled check |
| `actions.py` | Command replies, each scoped to the invoking channel (sync, Discord-agnostic) |
| `tracker.py` | Alert rules and the per-product check |
| `extract.py` | HTML → price |
| `shopify.py` | Price after automatic discounts, from a fresh Shopify cart |
| `fetch.py` | `PageFetcher`: Chrome-impersonating HTTP (`curl_cffi`), or headless Chromium (Playwright) for `render` products |
| `notify.py` | Posts alert embeds to a product's channel as the bot |
| `table.py` | Plain-text tables for `/list` |
| `db.py` | SQLite schema, append-only migrations (`PRAGMA user_version`), queries; prices stored as TEXT so `Decimal` values stay exact |
| `config.py` | Settings (pydantic-settings) |
