"""SQLAlchemy models — the schema's source of truth. Migrations are generated from these."""

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

# Deterministic names, so autogenerate is stable and later migrations can reference them.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_ID_LENGTH = 64  # fits "2401.12345" and "cond-mat/0211034" with room to spare
_NAME_LENGTH = 255


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Paper(Base):
    """One arXiv paper, identified by `arxiv_id`."""

    __tablename__ = "paper"

    id: Mapped[int] = mapped_column(primary_key=True)
    arxiv_id: Mapped[str] = mapped_column(String(_ID_LENGTH), unique=True)  # no "vN" suffix
    title: Mapped[str] = mapped_column(Text)
    abstract: Mapped[str] = mapped_column(Text)
    primary_category: Mapped[str] = mapped_column(String(_ID_LENGTH))
    published: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # drives the watermark
    doi: Mapped[str | None] = mapped_column(String(_NAME_LENGTH), default=None)
    journal_ref: Mapped[str | None] = mapped_column(Text, default=None)


class Author(Base):
    """A distinct author name — arXiv has no author ids, so the name is the identity."""

    __tablename__ = "author"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(_NAME_LENGTH), unique=True)


class Category(Base):
    """An arXiv subject category, e.g. `cs.AI`."""

    __tablename__ = "category"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(_ID_LENGTH), unique=True)


class PaperAuthor(Base):
    __tablename__ = "paper_author"
    __table_args__ = (PrimaryKeyConstraint("paper_id", "author_id"),)

    paper_id: Mapped[int] = mapped_column(ForeignKey("paper.id", ondelete="CASCADE"))
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(SmallInteger)  # the feed's author order


class PaperCategory(Base):
    __tablename__ = "paper_category"
    __table_args__ = (PrimaryKeyConstraint("paper_id", "category_id"),)

    paper_id: Mapped[int] = mapped_column(ForeignKey("paper.id", ondelete="CASCADE"))
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id", ondelete="CASCADE"))


class IngestState(Base):
    """Incremental watermark, keyed by the category set that produced it (PRD §6.5)."""

    __tablename__ = "ingest_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    categories_key: Mapped[str] = mapped_column(Text, unique=True)  # sorted, "cs.AI|cs.LG"
    watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True))
