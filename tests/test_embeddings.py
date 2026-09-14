"""Embedding staleness — the mechanism that keeps the RAG index honest (FR-1.7)."""

from datetime import timedelta

from sqlalchemy import func, select

from pipeline.embeddings import content_hash, embedding_text, refresh_embeddings
from pipeline.models import Embedding, Paper
from pipeline.upsert import upsert_papers
from tests.helpers import DEFAULT_TIME as NOW
from tests.helpers import FakeEmbedder, make_paper


def seed(session, papers):
    result = upsert_papers(session, papers)
    session.commit()
    return result.changed_paper_ids


def test_title_leads_the_embedded_text():
    """If truncation ever bites it must eat the abstract, never the title."""
    assert embedding_text("T", "A").startswith("T")


def test_hash_tracks_the_title_too():
    """A title-only revision must still trigger a re-embed."""
    assert content_hash(embedding_text("Old", "A")) != content_hash(embedding_text("New", "A"))


def test_new_papers_are_embedded(session):
    ids = seed(session, [make_paper("2409.00001"), make_paper("2409.00002")])
    embedder = FakeEmbedder()

    assert refresh_embeddings(session, ids, embedder) == 2
    session.commit()
    assert session.execute(select(func.count()).select_from(Embedding)).scalar_one() == 2


def test_unchanged_papers_are_not_re_embedded(session):
    """Re-embedding is free, but doing it needlessly hides real staleness in the logs."""
    ids = seed(session, [make_paper("2409.00001")])
    embedder = FakeEmbedder()
    refresh_embeddings(session, ids, embedder)
    session.commit()

    assert refresh_embeddings(session, ids, embedder) == 0
    assert len(embedder.calls) == 1


def test_a_changed_abstract_changes_the_vector(session):
    """FR-1.7's exact verification."""
    ids = seed(session, [make_paper("2409.00001", abstract="Original text.")])
    embedder = FakeEmbedder()
    refresh_embeddings(session, ids, embedder)
    session.commit()
    before = session.execute(select(Embedding.vector, Embedding.content_hash)).one()

    ids = seed(
        session,
        [
            make_paper(
                "2409.00001", abstract="Completely different.", updated=NOW + timedelta(days=1)
            )
        ],
    )
    assert refresh_embeddings(session, ids, embedder) == 1
    session.commit()

    after = session.execute(select(Embedding.vector, Embedding.content_hash)).one()
    assert after.content_hash != before.content_hash
    assert list(after.vector) != list(before.vector)
    assert session.execute(select(func.count()).select_from(Embedding)).scalar_one() == 1


def test_metadata_only_revision_does_not_re_embed(session):
    """A fixed journal_ref bumps `updated` but must not spend an embedding."""
    ids = seed(session, [make_paper("2409.00001")])
    embedder = FakeEmbedder()
    refresh_embeddings(session, ids, embedder)
    session.commit()

    ids = seed(
        session,
        [make_paper("2409.00001", journal_ref="Now published", updated=NOW + timedelta(days=1))],
    )
    assert refresh_embeddings(session, ids, embedder) == 0


def test_changing_the_model_invalidates_every_row(session):
    """The property that makes a provider swap a re-run rather than a migration script."""
    ids = seed(session, [make_paper("2409.00001"), make_paper("2409.00002")])
    refresh_embeddings(session, ids, FakeEmbedder("model-a"))
    session.commit()

    assert refresh_embeddings(session, ids, FakeEmbedder("model-b")) == 2
    session.commit()
    assert session.execute(select(Embedding.model).limit(1)).scalar_one() == "model-b"


def test_wiping_a_paper_removes_its_vector(session):
    """FR-1.8's named failure: orphaned vectors left behind."""
    ids = seed(session, [make_paper("2409.00001")])
    refresh_embeddings(session, ids, FakeEmbedder())
    session.commit()

    session.execute(Paper.__table__.delete())
    session.commit()
    assert session.execute(select(func.count()).select_from(Embedding)).scalar_one() == 0


def test_no_papers_is_a_no_op(session):
    embedder = FakeEmbedder()
    assert refresh_embeddings(session, [], embedder) == 0
    assert embedder.calls == []
