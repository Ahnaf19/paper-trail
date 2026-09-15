"""The three aggregation endpoints. HTTP concerns only — the SQL lives in the service."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.dependencies import SessionDep
from app.schemas.analytics import AuthorCount, CategoryCount, Granularity, PeriodPoint
from app.services import analytics

router = APIRouter(prefix="/stats", tags=["stats"])

# Bounded: an unbounded top-N is a free denial-of-service against a GROUP BY.
TopN = Annotated[int, Query(ge=1, le=100, description="How many rows to return.")]


@router.get("/papers-over-time", summary="Paper count by category over time")
async def read_papers_over_time(
    session: SessionDep,
    granularity: Granularity = Granularity.MONTH,
    category: Annotated[str | None, Query(description="Restrict to one category.")] = None,
    date_from: Annotated[date | None, Query(alias="from", description="Inclusive.")] = None,
    date_to: Annotated[date | None, Query(alias="to", description="Inclusive.")] = None,
) -> list[PeriodPoint]:
    # The only check no single-field validator can express. Free, so it runs before
    # the category lookup touches the database.
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "`from` is after `to`")

    if category is not None and not await analytics.category_exists(session, category):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no category {category!r} in this corpus — "
            "see GET /stats/top-categories for what is loaded",
        )

    return await analytics.papers_over_time(
        session,
        granularity=granularity,
        category=category,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/top-categories", summary="Top N categories by paper count")
async def read_top_categories(session: SessionDep, n: TopN = 10) -> list[CategoryCount]:
    return await analytics.top_categories(session, n=n)


@router.get("/top-authors", summary="Top N authors by paper count")
async def read_top_authors(session: SessionDep, n: TopN = 10) -> list[AuthorCount]:
    return await analytics.top_authors(session, n=n)
