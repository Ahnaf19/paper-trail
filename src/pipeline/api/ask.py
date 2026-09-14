"""POST /ask — retrieve, decide, then generate. The decision comes before the model."""

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from pipeline.api.schemas import Answer, Question, Source
from pipeline.config import settings
from pipeline.db import get_session
from pipeline.embeddings import Embedder
from pipeline.llm import LLMClient, LLMUnavailableError
from pipeline.prompts import (
    NO_ANSWER,
    SYSTEM_PROMPT,
    build_user_message,
    fit_context,
    render_context,
)
from pipeline.retrieval import RetrievedPaper, retrieve

router = APIRouter(tags=["ask"])


def get_embedder(request: Request) -> Embedder:
    """Loaded once at startup and held on app.state — never constructed per request."""
    return request.app.state.embedder


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


SessionDep = Annotated[AsyncSession, Depends(get_session)]
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]
LLMDep = Annotated[LLMClient, Depends(get_llm)]


def _sources(papers: list[RetrievedPaper]) -> list[Source]:
    return [Source(arxiv_id=p.arxiv_id, title=p.title, score=round(p.score, 4)) for p in papers]


@router.post("/ask", summary="Answer a question from the stored abstracts")
async def ask(
    payload: Question,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    response: Response,
) -> Answer:
    """Below the relevance threshold the model is never called, so it cannot invent (FR-5.5)."""
    question = payload.question

    # Encoding is CPU-bound. On the event loop it would stall every other request, which is
    # the exact async trap the repo standards call out.
    vector = await asyncio.to_thread(embedder.embed_query, question)
    hits = await retrieve(session, vector, k=settings.retrieval_top_k)
    relevant = [hit for hit in hits if hit.score >= settings.relevance_threshold]

    if not relevant:
        # A refusal and a failure are different events, and the logs must say which.
        top = f"{hits[0].score:.3f}" if hits else "no embeddings in the corpus"
        logger.info(
            "ask refused: chars={} best={} threshold={}",
            len(question),
            top,
            settings.relevance_threshold,
        )
        return Answer(answer=NO_ANSWER, sources=[])

    # Sources come from the papers that fit, not from everything that cleared the
    # threshold -- otherwise a large top-k would cite context the model never saw.
    used = fit_context(relevant, max_chars=settings.max_context_chars)
    sources = _sources(used)
    context = render_context(used)
    began = time.perf_counter()
    try:
        answer = await llm.complete(SYSTEM_PROMPT, build_user_message(question, context))
    except LLMUnavailableError as exc:
        # Retrieval worked. Returning what we found beats a 500 that throws it away.
        logger.warning("ask degraded after retrieval: {}", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Answer(
            answer=f"Could not generate an answer — {exc}. "
            "The most relevant papers are listed in sources.",
            sources=sources,
        )

    logger.info(
        "ask answered: chars={} best={:.3f} sources={} llm={:.0f}ms",
        len(question),
        used[0].score,
        len(sources),
        (time.perf_counter() - began) * 1000,
    )
    return Answer(answer=answer, sources=sources)
