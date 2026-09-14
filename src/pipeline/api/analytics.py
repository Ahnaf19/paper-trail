"""The aggregation endpoints. Every count is computed by Postgres, never in Python (FR-4.1)."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pipeline.api.schemas import AuthorCount, CategoryCount, Granularity, PeriodPoint
from pipeline.db import get_session
from pipeline.models import Author, Category, Paper, PaperAuthor, PaperCategory

router = APIRouter(prefix="/stats", tags=["stats"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
# Bounded: an unbounded top-N is a free denial-of-service against a GROUP BY.
TopN = Annotated[int, Query(ge=1, le=100, description="How many rows to return.")]

# The label a chart axis shows, per bucket. Bound as a parameter, never interpolated.
_PERIOD_FORMAT: Final[dict[Granularity, str]] = {
    Granularity.MONTH: "YYYY-MM",
    Granularity.YEAR: "YYYY",
}


def _midnight_utc(day: date) -> datetime:
    """Resolve a bare date against UTC, so a client's time zone cannot shift the buckets."""
    return datetime.combine(day, time.min, tzinfo=UTC)


@router.get("/papers-over-time", summary="Paper count by category over time")
async def papers_over_time(
    session: SessionDep,
    granularity: Annotated[Granularity, Query()] = Granularity.MONTH,
    category: Annotated[str | None, Query(description="Restrict to one category.")] = None,
    date_from: Annotated[date | None, Query(alias="from", description="Inclusive.")] = None,
    date_to: Annotated[date | None, Query(alias="to", description="Inclusive.")] = None,
) -> list[PeriodPoint]:
    """Keyed on `published` — when the paper entered the world, not when it was last edited.

    Periods with no papers are omitted rather than gap-filled; charting libraries handle a
    missing point, and a fabricated zero row is a claim we cannot make about a sparse corpus.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "`from` is after `to`")

    # date_trunc on a timestamptz silently uses the session's time zone; pinning it to UTC
    # is what makes the same corpus bucket identically for every caller.
    published_utc = func.timezone("UTC", Paper.published)
    period = func.to_char(
        func.date_trunc(granularity.value, published_utc), _PERIOD_FORMAT[granularity]
    ).label("period")

    statement = (
        select(period, Category.code.label("category"), func.count(Paper.id.distinct()))
        .join(PaperCategory, PaperCategory.paper_id == Paper.id)
        .join(Category, Category.id == PaperCategory.category_id)
        .group_by(period, Category.code)
        .order_by(period, Category.code)
    )
    if category is not None:
        statement = statement.where(Category.code == category)
    if date_from is not None:
        statement = statement.where(Paper.published >= _midnight_utc(date_from))
    if date_to is not None:
        # `to` names an inclusive day, so the bound is midnight at the start of the next.
        statement = statement.where(Paper.published < _midnight_utc(date_to + timedelta(days=1)))

    rows = (await session.execute(statement)).all()
    return [PeriodPoint(period=label, category=code, count=count) for label, code, count in rows]


@router.get("/top-categories", summary="Top N categories by paper count")
async def top_categories(session: SessionDep, n: TopN = 10) -> list[CategoryCount]:
    """Ties are broken by category code, so repeating the request repeats the rows."""
    count = func.count(PaperCategory.paper_id.distinct()).label("count")
    statement = (
        select(Category.code, count)
        .join(PaperCategory, PaperCategory.category_id == Category.id)
        .group_by(Category.code)
        .order_by(count.desc(), Category.code)
        .limit(n)
    )
    rows = (await session.execute(statement)).all()
    return [CategoryCount(category=code, count=total) for code, total in rows]


@router.get("/top-authors", summary="Top N authors by paper count")
async def top_authors(session: SessionDep, n: TopN = 10) -> list[AuthorCount]:
    """arXiv publishes no author ids, so the name is the identity — see the README."""
    count = func.count(PaperAuthor.paper_id.distinct()).label("count")
    statement = (
        select(Author.name, count)
        .join(PaperAuthor, PaperAuthor.author_id == Author.id)
        .group_by(Author.name)
        .order_by(count.desc(), Author.name)
        .limit(n)
    )
    rows = (await session.execute(statement)).all()
    return [AuthorCount(author=name, count=total) for name, total in rows]
