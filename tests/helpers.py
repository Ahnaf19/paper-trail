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


class KeywordEmbedder:
    """Text sharing a topic word maps to the same basis vector, so similarity is exactly 1 or 0.

    The hash-based FakeEmbedder is fine for "did it re-embed?", but two random 384-d vectors
    are near-orthogonal — so every retrieval test would sit below any sane threshold and the
    answered path would never be exercised. Here the score is the test's to choose.
    """

    TOPICS = ("transformer", "protein", "quantum")

    def __init__(self, model_id: str = "keyword-embedder/v1") -> None:
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMENSIONS
        lowered = text.lower()
        for index, topic in enumerate(self.TOPICS):
            if topic in lowered:
                vector[index] = 1.0
                return vector
        vector[-1] = 1.0  # the "about nothing we hold" axis
        return vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class StubLLM:
    """Records every call, so "the model was never asked" is an assertion, not a hope."""

    def __init__(self, reply: str = "A grounded answer.", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return self.reply
