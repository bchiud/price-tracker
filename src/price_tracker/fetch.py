import logging
from types import TracebackType
from typing import Self

from curl_cffi import requests as curl_requests
from playwright.sync_api import Browser, Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from price_tracker import shopify
from price_tracker.models import CartPrice, HTTPStatusError

log = logging.getLogger(__name__)

# For headless Chromium, whose default User-Agent says "HeadlessChrome".
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Without a selector to wait for, give page scripts this long after `load` to rewrite the price.
_SETTLE_MS = 3000


class PageFetcher:
    """Fetches page HTML over plain HTTP, or through headless Chromium when the price is drawn by JavaScript.

    Plain fetches impersonate Chrome down to the TLS handshake (curl_cffi). Bot protection like Akamai fingerprints
    that handshake, so a Python HTTP client with a Chrome User-Agent still gets 403; this gets the real page.

    Chromium starts on the first `render=True` fetch and is reused until `close()`; plain fetches never start it.
    Playwright's sync API is bound to the thread that started it, so create, use, and close this in one thread.
    """

    def __init__(self, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds
        self._timeout_ms = timeout_seconds * 1000
        self._session: curl_requests.Session[curl_requests.Response] | None = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    def __call__(self, url: str, *, render: bool = False, wait_for: str | None = None) -> str:
        if not render:
            return self._fetch(url)
        return self._render(url, wait_for)

    def _fetch(self, url: str) -> str:
        if self._session is None:
            # impersonate also sets matching headers (User-Agent etc.); overriding them would contradict the
            # fingerprint.
            self._session = curl_requests.Session(impersonate="chrome")
        response = self._session.get(url, timeout=self._timeout_seconds)
        if response.status_code >= 400:
            raise HTTPStatusError(response.status_code, url)
        return response.text

    def cart_price(self, url: str, html: str) -> CartPrice | None:
        """Shopify only: the price after automatic discounts, from a fresh cart. None for other stores."""
        if not shopify.is_product_page(url, html):
            return None
        # A new session each time: fresh cookies mean an empty cart, so the result is just this one item.
        with curl_requests.Session(impersonate="chrome") as session:
            return shopify.cart_price(session, url, self._timeout_seconds)

    def _render(self, url: str, wait_for: str | None) -> str:
        if self._browser is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch()
        # Fresh context per page: no cookies or cart state leak between products.
        context = self._browser.new_context(user_agent=_USER_AGENT, locale="en-US")
        try:
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            if response is not None and not response.ok:
                raise HTTPStatusError(response.status, url)
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=self._timeout_ms)
                except PlaywrightTimeoutError:
                    # e.g. a sale ended and the app no longer injects its price element; extract from what's there.
                    log.warning("Selector %r never appeared on %s; using the page as rendered", wait_for, url)
            else:
                page.wait_for_load_state("load", timeout=self._timeout_ms)
                page.wait_for_timeout(_SETTLE_MS)
            return page.content()
        finally:
            context.close()

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
