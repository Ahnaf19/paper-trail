"""Writes papers, authors, categories and their links. Knows the database, never saw a URL."""

from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Author, Category, Paper, PaperAuthor, PaperCategory
from app.schemas.arxiv import ArxivPaper


@dataclass
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    # Papers whose content moved -- what the report counts as updated.
    changed_paper_ids: list[int] = field(default_factory=list)
    # EVERY paper on the page. Embedding works from this, not from changed_paper_ids:
    # a paper stored before embeddings existed is "unchanged" on every future run and
    # would otherwise never get a vector.
    all_paper_ids: list[int] = field(default_factory=list)

    def __add__(self, other: "UpsertResult") -> "UpsertResult":
        return UpsertResult(
            self.inserted + other.inserted,
            self.updated + other.updated,
            self.unchanged + other.unchanged,
            self.changed_paper_ids + other.changed_paper_ids,
            self.all_paper_ids + other.all_paper_ids,
        )


def _resolve_lookup(session: Session, model: type, column: str, values: set[str]) -> dict[str, int]:
    """Get-or-create in two statements, whatever the value count — never one per row."""
    if not values:
        return {}
    session.execute(
        pg_insert(model)
        .values([{column: v} for v in sorted(values)])
        .on_conflict_do_nothing(index_elements=[column]),
    )
    rows = session.execute(
        select(getattr(model, column), model.id).where(getattr(model, column).in_(values))
    )
    return dict(rows.all())


def upsert_papers(session: Session, papers: list[ArxivPaper]) -> UpsertResult:
    """Insert or refresh a page of papers inside the caller's transaction.

    A paper is only rewritten when arXiv's `updated` is newer than ours, so a re-run
    over unchanged data touches nothing and RETURNING names exactly what moved.
    """
    if not papers:
        return UpsertResult()

    # Which of these we already hold, so the RETURNING set can be split into
    # inserts and updates without relying on Postgres internals like `xmax`.
    incoming = [p.arxiv_id for p in papers]
    existing_before = {
        row[0]
        for row in session.execute(select(Paper.arxiv_id).where(Paper.arxiv_id.in_(incoming)))
    }

    statement = pg_insert(Paper).values(
        [
            {
                "arxiv_id": p.arxiv_id,
                "title": p.title,
                "abstract": p.abstract,
                "primary_category": p.primary_category,
                "published": p.published,
                "updated": p.updated,
                "doi": p.doi,
                "journal_ref": p.journal_ref,
            }
            for p in papers
        ]
    )
    excluded = statement.excluded
    changed = session.execute(
        statement.on_conflict_do_update(
            index_elements=["arxiv_id"],
            set_={
                "title": excluded.title,
                "abstract": excluded.abstract,
                "primary_category": excluded.primary_category,
                "published": excluded.published,
                "updated": excluded.updated,
                "doi": excluded.doi,
                "journal_ref": excluded.journal_ref,
            },
            where=Paper.__table__.c.updated < excluded.updated,
        ).returning(Paper.id, Paper.arxiv_id)
    ).all()

    changed_ids = {arxiv_id: pk for pk, arxiv_id in changed}
    all_ids = dict(
        session.execute(select(Paper.arxiv_id, Paper.id).where(Paper.arxiv_id.in_(incoming))).all()
    )
    by_id = {p.arxiv_id: p for p in papers if p.arxiv_id in changed_ids}
    _replace_links(session, changed_ids, by_id)

    inserted = sum(1 for a in changed_ids if a not in existing_before)
    return UpsertResult(
        inserted=inserted,
        updated=len(changed_ids) - inserted,
        unchanged=len(papers) - len(changed_ids),
        changed_paper_ids=list(changed_ids.values()),
        all_paper_ids=list(all_ids.values()),
    )


def _replace_links(
    session: Session, paper_ids: dict[str, int], papers: dict[str, ArxivPaper]
) -> None:
    """Recompute a changed paper's links: delete then insert.

    Diffing would be more code and more bugs for no benefit at this size, and leaving
    them alone would silently double the link rows on every re-run.
    """
    if not paper_ids:
        return

    ids = list(paper_ids.values())
    session.execute(delete(PaperAuthor).where(PaperAuthor.paper_id.in_(ids)))
    session.execute(delete(PaperCategory).where(PaperCategory.paper_id.in_(ids)))

    author_ids = _resolve_lookup(
        session, Author, "name", {n for p in papers.values() for n in p.authors}
    )
    category_ids = _resolve_lookup(
        session, Category, "code", {c for p in papers.values() for c in p.categories}
    )

    author_links = [
        {"paper_id": paper_ids[a], "author_id": author_ids[name], "position": i}
        for a, paper in papers.items()
        for i, name in enumerate(paper.authors)
    ]
    category_links = [
        {"paper_id": paper_ids[a], "category_id": category_ids[code]}
        for a, paper in papers.items()
        for code in paper.categories
    ]
    if author_links:
        session.execute(pg_insert(PaperAuthor).values(author_links))
    if category_links:
        session.execute(pg_insert(PaperCategory).values(category_links))

    logger.debug("linked {} authors, {} categories", len(author_links), len(category_links))
