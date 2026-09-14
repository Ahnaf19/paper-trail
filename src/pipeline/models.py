"""SQLAlchemy models — the schema's single source of truth.

Alembic migrations are generated from these and reviewed by hand; the models are never
hand-drifted apart from the migrations.
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint and index names. Without this, autogenerate emits unnamed
# constraints that a later migration cannot reference, and produces noisy diffs.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# arXiv ids are short: "2401.12345" (new style) or "cond-mat/0211034" (old style).
# 64 is comfortable headroom. Category codes are shorter still ("cs.AI",
# "cond-mat.stat-mech"), but share the bound rather than inventing a second one.
_ID_LENGTH = 64
_NAME_LENGTH = 255


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Paper(Base):
    """One arXiv paper. Identity is `arxiv_id`, which is what makes re-ingest idempotent."""

    __tablename__ = "paper"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stored WITHOUT the version suffix: "2401.12345", never "2401.12345v2". A revision
    # must update the existing row rather than fork a second one (FR-1.6).
    arxiv_id: Mapped[str] = mapped_column(String(_ID_LENGTH), unique=True)
    title: Mapped[str] = mapped_column(Text)
    abstract: Mapped[str] = mapped_column(Text)
    primary_category: Mapped[str] = mapped_column(String(_ID_LENGTH))
    published: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Drives the incremental watermark (FR-1.6) and is what a revision bumps.
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    doi: Mapped[str | None] = mapped_column(String(_NAME_LENGTH), default=None)
    journal_ref: Mapped[str | None] = mapped_column(Text, default=None)


class Author(Base):
    """A distinct author name.

    arXiv publishes no author identifiers, so the name string IS the identity. Two
    different people sharing a name merge into one row — a real limitation, stated in
    the README rather than papered over.
    """

    __tablename__ = "author"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(_NAME_LENGTH), unique=True)


class Category(Base):
    """An arXiv subject category, e.g. `cs.AI`."""

    __tablename__ = "category"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(_ID_LENGTH), unique=True)


class PaperAuthor(Base):
    """Link table. `position` preserves the feed's author order, which is meaningful."""

    __tablename__ = "paper_author"
    __table_args__ = (PrimaryKeyConstraint("paper_id", "author_id"),)

    paper_id: Mapped[int] = mapped_column(ForeignKey("paper.id", ondelete="CASCADE"))
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(SmallInteger)


class PaperCategory(Base):
    """Link table. A paper's `primary_category` also appears here, as one of its categories."""

    __tablename__ = "paper_category"
    __table_args__ = (PrimaryKeyConstraint("paper_id", "category_id"),)

    paper_id: Mapped[int] = mapped_column(ForeignKey("paper.id", ondelete="CASCADE"))
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id", ondelete="CASCADE"))


class IngestState(Base):
    """The incremental watermark, keyed by the category set that produced it.

    A watermark is only valid for its own query. Ingesting `cs.AI`, then re-running with
    `cs.AI`+`cs.LG`, must NOT reuse the first mark — the early stop would fire and every
    older `cs.LG` paper would be silently skipped (PRD §6.5).
    """

    __tablename__ = "ingest_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Sorted and joined, e.g. "cs.AI|cs.CL|cs.LG".
    categories_key: Mapped[str] = mapped_column(Text, unique=True)
    watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True))
