"""Shared builders for test data."""

import hashlib
import random
from datetime import UTC, datetime

from pipeline.arxiv_parser import ArxivPaper
from pipeline.models import EMBEDDING_DIMENSIONS

DEFAULT_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def make_paper(
    arxiv_id: str = "2409.00001",
    *,
    published: datetime = DEFAULT_TIME,
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
        published=published,
        updated=updated,
        doi=kwargs.get("doi"),
        journal_ref=kwargs.get("journal_ref"),
    )


class FakeEmbedder:
    """Deterministic vectors derived from the text, so tests never load a model."""

    def __init__(self, model_id: str = "fake-model/v1") -> None:
        self._model_id = model_id
        self.calls: list[list[str]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    def _vector(self, text: str) -> list[float]:
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        return [rng.uniform(-1, 1) for _ in range(EMBEDDING_DIMENSIONS)]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)
