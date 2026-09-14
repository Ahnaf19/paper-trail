"""Client tests drive a stubbed transport — they never reach arXiv."""

import httpx
import pytest

from pipeline.arxiv_client import ArxivClient, ArxivUnavailableError, build_query
from pipeline.config import settings

FEED = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'


def _client(handler) -> ArxivClient:
    transport = httpx.MockTransport(handler)
    return ArxivClient(httpx.Client(transport=transport))


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """Assert on the delays rather than serving them — the suite must stay fast."""
    slept: list[float] = []
    monkeypatch.setattr("pipeline.arxiv_client.time.sleep", slept.append)
    return slept


def test_query_joins_categories_with_or():
    """`+` is arXiv's documented separator; sorting keeps the key stable (PRD §6.5)."""
    assert build_query(["cs.LG", "cs.AI"]) == "cat:cs.AI+OR+cat:cs.LG"


def test_pagination_advances_start_by_page_size():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["start"])
        return httpx.Response(200, text=FEED)

    pages = _client(handler).iter_pages("cat:cs.AI")
    for _ in range(3):
        next(pages)

    assert seen == ["0", str(settings.arxiv_page_size), str(settings.arxiv_page_size * 2)]


def test_sort_parameters_are_always_sent():
    """The whole watermark design depends on these two (PRD §6.5)."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(200, text=FEED)

    next(_client(handler).iter_pages("cat:cs.AI"))
    assert captured["sortBy"] == "lastUpdatedDate"
    assert captured["sortOrder"] == "descending"


def test_throttle_waits_between_requests(_no_real_sleeping):
    """FR-1.3 — the delay lives in the client, not in every caller."""
    pages = _client(lambda _: httpx.Response(200, text=FEED)).iter_pages("cat:cs.AI")
    next(pages)
    next(pages)
    assert _no_real_sleeping, "second request did not wait"
    assert max(_no_real_sleeping) <= settings.arxiv_min_request_interval


def test_429_is_retried_with_growing_backoff(_no_real_sleeping):
    """FR-1.5a — 429 is real and undocumented; we saw it repeatedly on 14 Sep."""
    attempts = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(200, text=FEED) if attempts["n"] > 3 else httpx.Response(429)

    _start, body = next(_client(handler).iter_pages("cat:cs.AI"))

    assert attempts["n"] == 4
    assert body == FEED
    # The throttle also sleeps, so match the backoff schedule exactly rather than
    # filtering by size: 2s, 4s, 8s. Retries wait the throttle too — we must not
    # exceed 1 req/3s while backing off either.
    backoffs = [d for d in _no_real_sleeping if d in (2.0, 4.0, 8.0)]
    assert backoffs == [2.0, 4.0, 8.0]


def test_retry_after_header_is_honoured(_no_real_sleeping):
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "17"})
        return httpx.Response(200, text=FEED)

    next(_client(handler).iter_pages("cat:cs.AI"))
    assert 17.0 in _no_real_sleeping


def test_exhausted_retries_raise_rather_than_return_junk():
    """FR-1.5a — exhaustion must be loud, so the CLI exits non-zero with the DB intact."""
    with pytest.raises(ArxivUnavailableError, match="start=0"):
        next(_client(lambda _: httpx.Response(429)).iter_pages("cat:cs.AI"))


def test_connection_errors_are_retried_too():
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    with pytest.raises(ArxivUnavailableError):
        next(_client(handler).iter_pages("cat:cs.AI"))


def test_client_errors_are_not_retried():
    """A 400 is our bug — retrying it just wastes arXiv's time."""
    with pytest.raises(httpx.HTTPStatusError):
        next(_client(lambda _: httpx.Response(400)).iter_pages("cat:cs.AI"))
