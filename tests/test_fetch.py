"""PageFetcher tests. The render tests drive real headless Chromium against a local HTTP server."""

import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from price_tracker.extract import extract_price
from price_tracker.fetch import PageFetcher
from price_tracker.models import HTTPStatusError

# Mimics a discount app: the server sends the full price, then a script swaps in the sale element later.
_JS_PRICED = """<!doctype html><html><head><meta charset="utf-8">
<script type="application/ld+json">{"@type": "Product", "offers": {"price": "160.00"}}</script>
</head><body><div id="price"><b>160,00 €</b></div>
<script>
  setTimeout(() => {
    document.getElementById("price").innerHTML = '<div id="sale"><b>96,00 €</b><s>160,00 €</s></div>';
  }, 500);
</script></body></html>"""


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    root: Path = tmp_path_factory.mktemp("site")
    (root / "product.html").write_text(_JS_PRICED)

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def fetcher() -> Iterator[PageFetcher]:
    with PageFetcher(timeout_seconds=5) as fetch:
        yield fetch


def test_plain_fetch_sees_only_server_html(site: str, fetcher: PageFetcher) -> None:
    html = fetcher(f"{site}/product.html")
    assert extract_price(html, "#sale b") == extract_price(html) == 160
    assert fetcher._browser is None  # plain fetches never start Chromium


def test_render_waits_for_selector_injected_by_js(site: str, fetcher: PageFetcher) -> None:
    html = fetcher(f"{site}/product.html", render=True, wait_for="#sale b")
    assert extract_price(html, "#sale b") == 96


def test_render_falls_back_when_selector_never_appears(site: str, caplog: pytest.LogCaptureFixture) -> None:
    with PageFetcher(timeout_seconds=1.5) as fetch:
        html = fetch(f"{site}/product.html", render=True, wait_for="#gone")
    # Sale over / element missing: the full price from structured data still comes through.
    assert extract_price(html, "#gone") == 160
    assert "never appeared" in caplog.text


def test_render_without_selector_lets_scripts_settle(site: str, fetcher: PageFetcher) -> None:
    html = fetcher(f"{site}/product.html", render=True)
    assert "96,00 €" in html


@pytest.mark.parametrize("render", [False, True])
def test_raises_on_http_error(site: str, fetcher: PageFetcher, render: bool) -> None:
    with pytest.raises(HTTPStatusError, match="HTTP 404") as raised:
        fetcher(f"{site}/missing.html", render=render)
    assert raised.value.status == 404


def test_plain_fetch_impersonates_chrome(fetcher: PageFetcher) -> None:
    # The point of curl_cffi: Chrome's headers (and TLS handshake), not a Python client's.
    received: dict[str, str] = {}

    class Echo(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            received.update(self.headers)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        fetcher(f"http://127.0.0.1:{server.server_address[1]}/")
    finally:
        server.shutdown()
    assert "Chrome/" in received["User-Agent"] and "python" not in received["User-Agent"].lower()
    assert "sec-ch-ua" in {k.lower() for k in received}


def test_close_is_idempotent_and_browser_restarts(site: str) -> None:
    fetch = PageFetcher(timeout_seconds=5)
    fetch(f"{site}/product.html", render=True, wait_for="#sale b")
    fetch(f"{site}/product.html")
    fetch.close()
    fetch.close()
    assert fetch._browser is None and fetch._session is None
    html = fetch(f"{site}/product.html", render=True, wait_for="#sale b")
    assert extract_price(html, "#sale b") == 96
    assert extract_price(fetch(f"{site}/product.html")) == 160
    fetch.close()
