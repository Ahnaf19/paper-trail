"""Drives the ingest loop: fetch -> parse -> upsert, one transaction per page."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.services.arxiv_client import ArxivClient, build_query
from app.services.arxiv_parser import parse_feed
from app.services.embeddings import Embedder, FastEmbedEmbedder, refresh_embeddings
from app.services.upsert import upsert_papers
from app.services.watermark import (
    categories_key,
    page_is_older_than,
    read_watermark,
    save_watermark,
    stop_threshold,
)


@dataclass
class IngestReport:
    pages_fetched: int = 0
    papers_seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    stopped_early: bool = False
    embedded: int = 0
    gap_left_open: bool = False
    changed_paper_ids: list[int] | None = None


def run_ingest(
    categories: list[str],
    *,
    max_papers: int,
    full: bool = False,
    client: ArxivClient | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
    embedder: Embedder | None = None,
) -> IngestReport:
    """Fetch until `max_papers`, the feed ends, or the watermark says everything below is old."""
    key = categories_key(categories)
    with session_factory() as session:
        mark = None if full else read_watermark(session, key)
    threshold = stop_threshold(mark)

    logger.info(
        "ingest categories={} max={} full={} watermark={}",
        key,
        max_papers,
        full,
        mark.isoformat() if mark else "none",
    )

    embedder = embedder or FastEmbedEmbedder()
    report = IngestReport(changed_paper_ids=[])
    newest_seen: datetime | None = None
    oldest_seen: datetime | None = None
    owns_client = client is None
    arxiv = client or ArxivClient()

    try:
        for start, xml in arxiv.iter_pages(build_query(categories)):
            page = parse_feed(xml)
            if not page.papers:
                logger.info("feed returned no entries at start={}; done", start)
                break

            # One transaction per page: a crash loses this page, never the run. The
            # embeddings are written inside it too, so a paper can never be committed
            # without its vector (PRD §6.1).
            with session_factory() as session:
                result = upsert_papers(session, page.papers)
                embedded = refresh_embeddings(session, result.all_paper_ids, embedder)
                session.commit()

            report.pages_fetched += 1
            report.papers_seen += len(page.papers)
            report.inserted += result.inserted
            report.updated += result.updated
            report.unchanged += result.unchanged
            report.changed_paper_ids.extend(result.changed_paper_ids)
            report.embedded += embedded

            page_newest = max(p.updated for p in page.papers)
            page_oldest = min(p.updated for p in page.papers)
            newest_seen = page_newest if newest_seen is None else max(newest_seen, page_newest)
            oldest_seen = page_oldest if oldest_seen is None else min(oldest_seen, page_oldest)
            logger.info(
                "page start={} papers={} new={} updated={} unchanged={} (total {})",
                start,
                len(page.papers),
                result.inserted,
                result.updated,
                result.unchanged,
                report.papers_seen,
            )

            if page_is_older_than(page.papers, threshold):
                logger.info("page is entirely older than the watermark; stopping early")
                report.stopped_early = True
                break
            if report.papers_seen >= max_papers:
                logger.info("reached max={}", max_papers)
                break
            if start + len(page.papers) >= page.total_results:
                logger.info("reached the end of the result set")
                break
    finally:
        if owns_client:
            arxiv.close()

    # Only after the loop finishes cleanly — an exception must not record progress we
    # did not make, or the next run would stop early over a gap.
    if newest_seen is not None and oldest_seen is not None:
        with session_factory() as session:
            report.gap_left_open = not _advance_watermark(
                session, key, newest=newest_seen, oldest=oldest_seen
            )
            session.commit()

    return report


def _advance_watermark(session: Session, key: str, *, newest: datetime, oldest: datetime) -> bool:
    """Move the mark to `newest` only if this run's span joins onto what we already held.

    The mark asserts "everything at or above this is ingested". That holds for `newest`
    only when the walk came down far enough to meet the previous mark. A run cut short by
    `--max` can stop above it, leaving a span that was never fetched — and advancing over
    that span would bury it permanently below the next run's stop threshold.

    Returns whether the mark moved.
    """
    previous = read_watermark(session, key)
    if previous is None or oldest <= previous:
        save_watermark(session, key, newest)
        return True

    logger.warning(
        "gap left open: this run reached back only to {}, still above the watermark {}. "
        "Leaving the mark where it is — re-run with a larger --max (or --full) to close it.",
        oldest.isoformat(),
        previous.isoformat(),
    )
    return False
