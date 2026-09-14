"""Drives the ingest loop: fetch -> parse -> upsert, one transaction per page."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from sqlalchemy.orm import Session

from pipeline.arxiv_client import ArxivClient, build_query
from pipeline.arxiv_parser import parse_feed
from pipeline.db import SessionLocal
from pipeline.upsert import upsert_papers
from pipeline.watermark import (
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
    changed_paper_ids: list[int] | None = None


def run_ingest(
    categories: list[str],
    *,
    max_papers: int,
    full: bool = False,
    client: ArxivClient | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
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

    report = IngestReport(changed_paper_ids=[])
    newest_seen: datetime | None = None
    owns_client = client is None
    arxiv = client or ArxivClient()

    try:
        for start, xml in arxiv.iter_pages(build_query(categories)):
            page = parse_feed(xml)
            if not page.papers:
                logger.info("feed returned no entries at start={}; done", start)
                break

            # One transaction per page: a crash loses this page, never the run.
            with session_factory() as session:
                result = upsert_papers(session, page.papers)
                session.commit()

            report.pages_fetched += 1
            report.papers_seen += len(page.papers)
            report.inserted += result.inserted
            report.updated += result.updated
            report.unchanged += result.unchanged
            report.changed_paper_ids.extend(result.changed_paper_ids)

            page_newest = max(p.updated for p in page.papers)
            newest_seen = page_newest if newest_seen is None else max(newest_seen, page_newest)
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
    if newest_seen is not None:
        with session_factory() as session:
            save_watermark(session, key, newest_seen)
            session.commit()

    return report
