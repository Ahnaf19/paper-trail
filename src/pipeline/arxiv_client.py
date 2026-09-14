"""Fetches pages from arXiv. Knows HTTP and throttling; knows nothing about Atom."""

import time
from collections.abc import Iterator
from typing import Final

import httpx
from loguru import logger

from pipeline.config import settings

_BACKOFF_BASE_SECONDS: Final = 2.0
_RETRYABLE_STATUS: Final = frozenset({429, 500, 502, 503, 504})
# The caller breaks out of iter_pages; this only stops a runaway loop if it forgets.
_MAX_PAGES: Final = 500


class ArxivUnavailableError(RuntimeError):
    """arXiv could not be reached within the configured retries."""


def build_query(categories: list[str]) -> str:
    """`cat:cs.AI+OR+cat:cs.LG` — `+` is arXiv's documented separator, not an encoded space."""
    return "+OR+".join(f"cat:{code}" for code in sorted(categories))


class ArxivClient:
    """One connection, one request per `arxiv_min_request_interval` (arXiv's Terms of Use)."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            timeout=settings.arxiv_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": settings.arxiv_user_agent},
        )
        self._last_request_at: float | None = None

    def _wait_turn(self) -> None:
        if self._last_request_at is None:
            return
        remaining = settings.arxiv_min_request_interval - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def _get(self, query: str, start: int, page_size: int) -> str:
        url = (
            f"{settings.arxiv_base_url}?search_query={query}"
            f"&start={start}&max_results={page_size}"
            f"&sortBy=lastUpdatedDate&sortOrder=descending"
        )
        for attempt in range(settings.arxiv_max_retries + 1):
            self._wait_turn()
            try:
                response = self._client.get(url)
                self._last_request_at = time.monotonic()
                if response.status_code not in _RETRYABLE_STATUS:
                    response.raise_for_status()
                    return response.text
                reason = f"HTTP {response.status_code}"
                retry_after = _retry_after_seconds(response)
            except httpx.RequestError as exc:
                self._last_request_at = time.monotonic()
                reason, retry_after = repr(exc), None

            if attempt == settings.arxiv_max_retries:
                break
            delay = retry_after or _BACKOFF_BASE_SECONDS * (2**attempt)
            logger.warning(
                "arxiv {} at start={}; retry {}/{} in {:.0f}s",
                reason,
                start,
                attempt + 1,
                settings.arxiv_max_retries,
                delay,
            )
            time.sleep(delay)

        raise ArxivUnavailableError(
            f"giving up at start={start} after {settings.arxiv_max_retries} retries"
        )

    def iter_pages(self, query: str, *, start: int = 0) -> Iterator[tuple[int, str]]:
        """Yield `(start, raw_xml)` per page. The caller decides when to stop."""
        page_size = settings.arxiv_page_size
        for _ in range(_MAX_PAGES):
            logger.info("fetching arxiv page start={} size={}", start, page_size)
            yield start, self._get(query, start, page_size)
            start += page_size
        logger.error("hit the {}-page safety bound; caller never stopped", _MAX_PAGES)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ArxivClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None
