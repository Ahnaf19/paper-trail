"""Shared builders for test data."""

from datetime import UTC, datetime

from pipeline.arxiv_parser import ArxivPaper

DEFAULT_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def make_paper(
    arxiv_id: str = "2409.00001",
    *,
    updated: datetime = DEFAULT_TIME,
    authors: list[str] | None = None,
    categories: list[str] | None = None,
    **kwargs: object,
) -> ArxivPaper:
    categories = categories or ["cs.AI"]
    return ArxivPaper(
        arxiv_id=arxiv_id,
        title=kwargs.get("title", "A Title"),
        abstract=kwargs.get("abstract", "An abstract."),
        primary_category=categories[0],
        categories=categories,
        authors=["Ada Lovelace"] if authors is None else authors,
        published=DEFAULT_TIME,
        updated=updated,
        doi=kwargs.get("doi"),
        journal_ref=kwargs.get("journal_ref"),
    )
