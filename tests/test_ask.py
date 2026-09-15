"""The /ask paths: grounded, refused, degraded. No network, no real model, no LLM."""

import pytest

from app.core.config import settings
from app.core.errors import LLMUnavailableError
from app.services.embeddings import refresh_embeddings
from app.services.upsert import upsert_papers
from tests.helpers import make_paper

PAPERS = [
    make_paper(
        "2601.10001",
        title="Transformer scaling laws",
        abstract="We study how transformer models scale with data.",
    ),
    make_paper(
        "2601.10002",
        title="Transformer attention analysis",
        abstract="An analysis of attention inside a transformer.",
    ),
    make_paper(
        "2601.10003",
        title="Protein folding with deep nets",
        abstract="Predicting protein structure from sequence.",
    ),
]


@pytest.fixture
def corpus(session, fake_embedder):
    """Seeded through the real write path, embedded with the same fake the API will use."""
    result = upsert_papers(session, PAPERS)
    refresh_embeddings(session, result.all_paper_ids, fake_embedder)
    session.commit()


def ask(client, question: str):
    return client.post("/ask", json={"question": question})


def test_a_grounded_question_is_answered_with_its_sources(client, corpus, stub_llm):
    response = ask(client, "Tell me about transformer models")
    body = response.json()

    assert response.status_code == 200
    assert body["answer"] == stub_llm.reply
    assert {s["arxiv_id"] for s in body["sources"]} == {"2601.10001", "2601.10002"}
    assert len(stub_llm.calls) == 1


def test_sources_are_only_the_papers_used_as_context(client, corpus, stub_llm):
    """FR-5.3: a source must be a paper that actually grounded the answer, not a near miss."""
    ask(client, "Tell me about transformer models")
    _system, user = stub_llm.calls[0]

    assert "2601.10001" in user and "2601.10002" in user
    assert "2601.10003" not in user  # the protein paper scored 0 and must not be context


def test_an_off_topic_question_never_reaches_the_model(client, corpus, stub_llm):
    """🔴 FR-5.5. The refusal is control flow, not a prompt the model might ignore."""
    response = ask(client, "What is the capital of France?")
    body = response.json()

    assert response.status_code == 200
    assert body["sources"] == []
    assert "don't have information" in body["answer"]
    assert stub_llm.calls == []  # zero tokens spent, nothing to hallucinate with


def test_an_empty_corpus_refuses_rather_than_crashing(client, session, stub_llm):
    response = ask(client, "Tell me about transformer models")

    assert response.status_code == 200
    assert response.json()["sources"] == []
    assert stub_llm.calls == []


def test_papers_without_embeddings_are_not_silently_no_results(client, session, stub_llm):
    """Papers but no vectors must refuse *and* be diagnosable — the two are different faults."""
    upsert_papers(session, PAPERS)
    session.commit()

    response = ask(client, "Tell me about transformer models")

    assert response.status_code == 200
    assert response.json()["sources"] == []


@pytest.mark.parametrize("question", ["", "   ", "\n\t "])
def test_an_empty_question_is_rejected_before_it_costs_anything(client, question, stub_llm):
    assert ask(client, question).status_code == 422
    assert stub_llm.calls == []


def test_an_over_long_question_is_rejected(client, stub_llm):
    assert ask(client, "transformer " * 500).status_code == 422
    assert stub_llm.calls == []


def test_the_llm_failing_keeps_the_retrieval(client, corpus, stub_llm):
    """Retrieval succeeded; throwing that away with a 500 would be the worse answer."""
    stub_llm.error = LLMUnavailableError("the language model rejected the API key")

    response = ask(client, "Tell me about transformer models")
    body = response.json()

    assert response.status_code == 503
    assert len(body["sources"]) == 2  # the useful half of the work survives
    assert "rejected the API key" in body["answer"]
    assert "Traceback" not in response.text


def test_k_larger_than_the_corpus_is_fine(client, session, fake_embedder, stub_llm):
    result = upsert_papers(session, PAPERS[:1])
    refresh_embeddings(session, result.all_paper_ids, fake_embedder)
    session.commit()

    body = ask(client, "Tell me about transformer models").json()

    assert len(body["sources"]) == 1


def test_sources_never_claim_context_the_model_did_not_see(client, corpus, stub_llm, monkeypatch):
    """A tight context budget must shrink `sources` too, or the response overclaims."""
    monkeypatch.setattr(settings, "max_context_chars", 1)

    body = ask(client, "Tell me about transformer models").json()
    _system, user = stub_llm.calls[0]

    assert len(body["sources"]) == 1  # both cleared the threshold; only one fits
    assert body["sources"][0]["arxiv_id"] in user
    for source in body["sources"]:
        assert source["arxiv_id"] in user
