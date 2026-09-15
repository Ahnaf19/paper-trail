"""Papers, their authors and categories, and the link tables joining them."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, PrimaryKeyConstraint, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import ID_LENGTH, NAME_LENGTH, Base


class Paper(Base):
    """One arXiv paper, identified by `arxiv_id`."""

    __tablename__ = "paper"

    id: Mapped[int] = mapped_column(primary_key=True)
    arxiv_id: Mapped[str] = mapped_column(String(ID_LENGTH), unique=True)  # no "vN" suffix
    title: Mapped[str] = mapped_column(Text)
    abstract: Mapped[str] = mapped_column(Text)
    primary_category: Mapped[str] = mapped_column(String(ID_LENGTH))
    published: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # drives the watermark
    doi: Mapped[str | None] = mapped_column(String(NAME_LENGTH), default=None)
    journal_ref: Mapped[str | None] = mapped_column(Text, default=None)


class Author(Base):
    """A distinct author name — arXiv has no author ids, so the name is the identity."""

    __tablename__ = "author"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_LENGTH), unique=True)


class Category(Base):
    """An arXiv subject category, e.g. `cs.AI`."""

    __tablename__ = "category"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(ID_LENGTH), unique=True)


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
