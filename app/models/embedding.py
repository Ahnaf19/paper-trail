"""One vector per paper, plus what makes it stale."""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import NAME_LENGTH, Base

# Pinned in the schema, not config: changing it is a migration plus a full re-embed.
EMBEDDING_DIMENSIONS = 384


class Embedding(Base):
    """Written in the same transaction as the paper, so a stale vector is impossible (§6.1)."""

    __tablename__ = "embedding"

    paper_id: Mapped[int] = mapped_column(
        ForeignKey("paper.id", ondelete="CASCADE"), primary_key=True
    )
    vector: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    content_hash: Mapped[str] = mapped_column(String(64))  # sha256 of the embedded text
    model: Mapped[str] = mapped_column(String(NAME_LENGTH))  # a model change invalidates the row
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
