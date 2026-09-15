"""Wire shapes for the analytics API — flat labelled points a chart plots directly (FR-4.4)."""

from enum import StrEnum

from pydantic import BaseModel, Field


class Granularity(StrEnum):
    """The `date_trunc` bucket. An enum, so request text never reaches the query."""

    MONTH = "month"
    YEAR = "year"


# Field order is the JSON key order: label first, then the number, as a chart reads it.
_COUNT = Field(description="Distinct papers in this bucket.")


class PeriodPoint(BaseModel):
    """One (period, category) bucket of the time series."""

    period: str = Field(description="`YYYY-MM` for month, `YYYY` for year.", examples=["2026-01"])
    category: str = Field(examples=["cs.AI"])
    count: int = _COUNT


class CategoryCount(BaseModel):
    category: str = Field(examples=["cs.LG"])
    count: int = _COUNT


class AuthorCount(BaseModel):
    author: str = Field(examples=["Ada Lovelace"])
    count: int = _COUNT
