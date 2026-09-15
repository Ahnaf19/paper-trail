"""Similarity search over stored abstracts. Ordering and metadata come from one query."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Embedding, Paper


@dataclass(frozen=True)
class RetrievedPaper:
    """A paper and how close it was. `score` is cosine similarity in [-1, 1]."""

    arxiv_id: str
    title: str
    abstract: str
    score: float


async def retrieve(
    session: AsyncSession, query_vector: list[float], *, k: int
) -> list[RetrievedPaper]:
    """Top-k nearest abstracts, with the paper metadata they belong to.

    One statement, not top-k-then-look-up: a source is a row that was joined, so it cannot
    fail to resolve (FR-5.3). pgvector's `<=>` is cosine distance, so similarity is 1 - it.
    """
    distance = Embedding.vector.cosine_distance(query_vector)
    statement = (
        select(Paper.arxiv_id, Paper.title, Paper.abstract, (1 - distance).label("score"))
        .join(Embedding, Embedding.paper_id == Paper.id)
        .order_by(distance)
        .limit(k)
    )
    rows = (await session.execute(statement)).all()
    return [
        RetrievedPaper(arxiv_id=arxiv_id, title=title, abstract=abstract, score=float(score))
        for arxiv_id, title, abstract, score in rows
    ]
