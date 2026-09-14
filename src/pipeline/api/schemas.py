"""Wire shapes for the analytics API — flat labelled points a chart plots directly (FR-4.4)."""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from pipeline.config import settings


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


# Stripped then length-checked, so "   " is a 422 rather than an embedding call on nothing.
QuestionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=settings.max_question_chars),
]


class Question(BaseModel):
    question: QuestionText = Field(examples=["How do LLM agents reuse past experience?"])


class Source(BaseModel):
    """A paper that was passed to the model as context -- never a paper that merely ranked."""

    arxiv_id: str = Field(examples=["2509.01234"])
    title: str
    score: float = Field(description="Cosine similarity to the question.")


class Answer(BaseModel):
    answer: str
    sources: list[Source]
