"""The aggregation queries. Every count is computed by Postgres, never in Python (FR-4.1)."""

from datetime import date
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Author, Category, Paper, PaperAuthor, PaperCategory
from app.schemas.analytics import AuthorCount, CategoryCount, Granularity, PeriodPoint
from app.utils.dates import end_of_day_utc, midnight_utc

# The label a chart axis shows, per bucket. Bound as a parameter, never interpolated.
_PERIOD_FORMAT: Final[dict[Granularity, str]] = {
    Granularity.MONTH: "YYYY-MM",
    Granularity.YEAR: "YYYY",
}


async def papers_over_time(
    session: AsyncSession,
    *,
    granularity: Granularity,
    category: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[PeriodPoint]:
    """Keyed on `published` — when the paper entered the world, not when it was last edited.

    Periods with no papers are omitted rather than gap-filled; charting libraries handle a
    missing point, and a fabricated zero row is a claim we cannot make about a sparse corpus.
    """
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
        statement = statement.where(Paper.published >= midnight_utc(date_from))
    if date_to is not None:
        # `to` names an inclusive day, so the bound is the start of the next one.
        statement = statement.where(Paper.published < end_of_day_utc(date_to))

    rows = (await session.execute(statement)).all()
    return [PeriodPoint(period=label, category=code, count=count) for label, code, count in rows]


async def top_categories(session: AsyncSession, *, n: int) -> list[CategoryCount]:
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


async def top_authors(session: AsyncSession, *, n: int) -> list[AuthorCount]:
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
