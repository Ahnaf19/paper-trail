"""Parsed arXiv records. Untrusted XML becomes one of these immediately, or not at all."""

from datetime import datetime

from pydantic import BaseModel


class ArxivPaper(BaseModel):
    """One paper, flattened and cleaned."""

    arxiv_id: str
    title: str
    abstract: str
    primary_category: str
    categories: list[str]
    authors: list[str]
    published: datetime
    updated: datetime
    doi: str | None = None
    journal_ref: str | None = None


class ArxivPage(BaseModel):
    """One response. `total_results` lets the caller bound its pagination."""

    total_results: int
    start_index: int
    papers: list[ArxivPaper]
