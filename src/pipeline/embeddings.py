"""Generates embeddings and decides which papers are stale."""

import hashlib
from typing import Protocol

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pipeline.config import settings
from pipeline.models import Embedding, Paper


def embedding_text(title: str, abstract: str) -> str:
    """Title first, so truncation eats the abstract's tail rather than the title."""
    return f"{title}\n{abstract}"


def content_hash(text: str) -> str:
    """A change detector, never a retrieval artifact — similarity search never sees it."""
    return hashlib.sha256(text.encode()).hexdigest()


class Embedder(Protocol):
    """The seam that keeps tests off the network."""

    @property
    def model_id(self) -> str: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedEmbedder:
    """ONNX Runtime, no PyTorch. Loaded lazily — construction must stay cheap."""

    def __init__(self, model_id: str | None = None) -> None:
        self._model_id = model_id or settings.embedding_model
        self._model = None

    @property
    def model_id(self) -> str:
        return self._model_id

    def _loaded(self):  # noqa: ANN202  -- fastembed exports no usable public type
        if self._model is None:
            from fastembed import TextEmbedding

            logger.info("loading embedding model {}", self._model_id)
            self._model = TextEmbedding(model_name=self._model_id)
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = self._loaded().embed(texts, batch_size=settings.embedding_batch_size)
        return [vector.tolist() for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        # bge is asymmetric: the query needs its instruction prefix, documents do not.
        # Getting this wrong degrades retrieval silently.
        return next(iter(self._loaded().query_embed(text))).tolist()


def refresh_embeddings(session: Session, paper_ids: list[int], embedder: Embedder) -> int:
    """Re-embed the papers whose embedded text changed. Runs inside the caller's transaction.

    Same transaction as the paper write, so a crash between the two is not handled —
    it is impossible (PRD §6.1).
    """
    if not paper_ids:
        return 0

    rows = session.execute(
        select(Paper.id, Paper.title, Paper.abstract, Embedding.content_hash, Embedding.model)
        .outerjoin(Embedding, Embedding.paper_id == Paper.id)
        .where(Paper.id.in_(paper_ids))
    ).all()

    stale: list[tuple[int, str, str]] = []
    for paper_id, title, abstract, stored_hash, stored_model in rows:
        text = embedding_text(title, abstract)
        digest = content_hash(text)
        # The model id is part of the check, so swapping models invalidates every row
        # through the same mechanism rather than needing a separate migration step.
        if stored_hash == digest and stored_model == embedder.model_id:
            continue
        stale.append((paper_id, text, digest))

    if not stale:
        return 0

    vectors = embedder.embed_documents([text for _, text, _ in stale])
    statement = pg_insert(Embedding).values(
        [
            {
                "paper_id": paper_id,
                "vector": vector,
                "content_hash": digest,
                "model": embedder.model_id,
            }
            for (paper_id, _, digest), vector in zip(stale, vectors, strict=True)
        ]
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["paper_id"],
            set_={
                "vector": statement.excluded.vector,
                "content_hash": statement.excluded.content_hash,
                "model": statement.excluded.model,
            },
        )
    )
    logger.info("embedded {} of {} changed papers", len(stale), len(paper_ids))
    return len(stale)
