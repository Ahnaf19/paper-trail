"""Decides where an incremental run starts and when it can stop early."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pipeline.arxiv_parser import ArxivPaper
from pipeline.config import settings
from pipeline.models import IngestState


def categories_key(categories: list[str]) -> str:
    """Sorted, so the same set always yields the same key regardless of argument order."""
    return "|".join(sorted(categories))


def read_watermark(session: Session, key: str) -> datetime | None:
    """The newest `updated` held for this exact category set.

    Keyed rather than global: a mark earned by `cs.AI` says nothing about `cs.LG`, and
    reusing it would stop the scan early and silently skip every older paper in the
    newly added category (PRD §6.5).
    """
    return session.execute(
        select(IngestState.watermark).where(IngestState.categories_key == key)
    ).scalar_one_or_none()


def save_watermark(session: Session, key: str, newest: datetime) -> None:
    """Record the newest `updated` seen, but never move the mark backwards."""
    statement = pg_insert(IngestState).values(categories_key=key, watermark=newest)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["categories_key"],
            set_={"watermark": statement.excluded.watermark},
            where=IngestState.__table__.c.watermark < statement.excluded.watermark,
        )
    )


def stop_threshold(mark: datetime | None) -> datetime | None:
    """The mark minus a safety margin — the feed shifts between paginated requests."""
    if mark is None:
        return None
    return mark - timedelta(days=settings.arxiv_watermark_margin_days)


def page_is_older_than(papers: list[ArxivPaper], threshold: datetime | None) -> bool:
    """True when nothing on this page is new to us.

    The feed is sorted by `lastUpdatedDate` descending, so once a whole page falls below
    the threshold everything after it does too — there is nothing further down we lack.
    """
    if threshold is None or not papers:
        return False
    return all(paper.updated <= threshold for paper in papers)
