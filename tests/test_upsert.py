"""Upsert tests against a real database — the behaviour being tested is Postgres's."""

from datetime import timedelta

from sqlalchemy import func, select

from app.models import Author, Category, Paper, PaperAuthor, PaperCategory
from app.services.upsert import upsert_papers
from tests.helpers import DEFAULT_TIME as NOW
from tests.helpers import make_paper


def count(session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_first_run_inserts(session):
    result = upsert_papers(session, [make_paper()])
    session.commit()
    assert (result.inserted, result.updated, result.unchanged) == (1, 0, 0)
    assert count(session, Paper) == 1


def test_rerunning_identical_data_changes_nothing(session):
    """FR-1.4 — the core idempotency guarantee."""
    papers = [make_paper("2409.00001"), make_paper("2409.00002")]
    upsert_papers(session, papers)
    session.commit()

    result = upsert_papers(session, papers)
    session.commit()

    assert (result.inserted, result.updated, result.unchanged) == (0, 0, 2)
    assert count(session, Paper) == 2


def test_rerunning_does_not_duplicate_link_rows(session):
    """The trap: papers dedupe correctly while link rows silently double each run."""
    papers = [make_paper(authors=["A", "B", "C"], categories=["cs.AI", "cs.LG"])]
    for _ in range(3):
        upsert_papers(session, papers)
        session.commit()

    assert count(session, PaperAuthor) == 3
    assert count(session, PaperCategory) == 2
    assert count(session, Author) == 3


def test_a_newer_updated_refreshes_the_row(session):
    """FR-1.6 — a revision must update in place, not insert a second row."""
    upsert_papers(session, [make_paper(title="Old title")])
    session.commit()

    result = upsert_papers(
        session, [make_paper(title="New title", updated=NOW + timedelta(days=1))]
    )
    session.commit()

    assert (result.inserted, result.updated) == (0, 1)
    assert count(session, Paper) == 1
    assert session.execute(select(Paper.title)).scalar_one() == "New title"


def test_an_older_updated_is_ignored(session):
    """Feed drift can re-serve a stale copy; it must not overwrite newer data."""
    upsert_papers(session, [make_paper(title="Current", updated=NOW)])
    session.commit()

    result = upsert_papers(session, [make_paper(title="Stale", updated=NOW - timedelta(days=5))])
    session.commit()

    assert result.unchanged == 1
    assert session.execute(select(Paper.title)).scalar_one() == "Current"


def test_changed_paper_ids_name_exactly_what_moved(session):
    """M4 re-embeds from this list, so it must not over- or under-report."""
    upsert_papers(session, [make_paper("2409.00001"), make_paper("2409.00002")])
    session.commit()

    result = upsert_papers(
        session,
        [
            make_paper("2409.00001", updated=NOW + timedelta(days=1)),
            make_paper("2409.00002"),
            make_paper("2409.00003"),
        ],
    )
    session.commit()

    assert len(result.changed_paper_ids) == 2
    assert result.unchanged == 1


def test_authors_are_shared_not_duplicated_across_papers(session):
    upsert_papers(
        session,
        [
            make_paper("2409.00001", authors=["Shared Name", "Only Here"]),
            make_paper("2409.00002", authors=["Shared Name"]),
        ],
    )
    session.commit()
    assert count(session, Author) == 2
    assert count(session, PaperAuthor) == 3


def test_author_order_is_preserved(session):
    upsert_papers(session, [make_paper(authors=["First", "Second", "Third"])])
    session.commit()
    rows = (
        session.execute(
            select(Author.name)
            .join(PaperAuthor, PaperAuthor.author_id == Author.id)
            .order_by(PaperAuthor.position)
        )
        .scalars()
        .all()
    )
    assert rows == ["First", "Second", "Third"]


def test_a_paper_in_two_categories_keeps_both(session):
    """Ingesting cs.AI and cs.LG returns overlapping papers — one row, both links."""
    upsert_papers(session, [make_paper(categories=["cs.AI", "cs.LG", "stat.ML"])])
    session.commit()
    assert count(session, Category) == 3
    assert count(session, PaperCategory) == 3
    assert count(session, Paper) == 1


def test_revised_paper_loses_a_dropped_author(session):
    """Links are recomputed, so a removed author must actually disappear."""
    upsert_papers(session, [make_paper(authors=["Kept", "Dropped"])])
    session.commit()

    upsert_papers(session, [make_paper(authors=["Kept"], updated=NOW + timedelta(days=1))])
    session.commit()

    assert count(session, PaperAuthor) == 1
    assert count(session, Author) == 2  # the author row survives; only the link goes


def test_missing_doi_and_journal_ref_store_as_null(session):
    """FR-2.3 — the 98% case in real data."""
    upsert_papers(session, [make_paper(doi=None, journal_ref=None)])
    session.commit()
    row = session.execute(select(Paper.doi, Paper.journal_ref)).one()
    assert row == (None, None)


def test_empty_page_is_a_no_op(session):
    result = upsert_papers(session, [])
    assert (result.inserted, result.updated, result.unchanged) == (0, 0, 0)
