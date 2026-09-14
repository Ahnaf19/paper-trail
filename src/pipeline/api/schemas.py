"""Wire shapes for the analytics API — flat labelled points a chart plots directly (FR-4.4)."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Granularity(StrEnum):
    """The `date_trunc` bucket. An enum, so request text never reaches the query."""

    MONTH = "month"
    YEAR = "year"


class _Point(BaseModel):
    """Built straight from a result row, so the query's labels are the JSON keys."""

    model_config = ConfigDict(from_attributes=True)

    count: int = Field(description="Distinct papers in this bucket.")


class PeriodPoint(_Point):
    """One (period, category) bucket of the time series."""

    period: str = Field(description="`YYYY-MM` for month, `YYYY` for year.", examples=["2026-01"])
    category: str = Field(examples=["cs.AI"])


class CategoryCount(_Point):
    category: str = Field(examples=["cs.LG"])


class AuthorCount(_Point):
    author: str = Field(examples=["Ada Lovelace"])
