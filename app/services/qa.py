"""Answering a question from the corpus: retrieve, decide, then generate.

The decision comes before the model. That ordering is the requirement (FR-5.5), not a
detail — so it lives here in one readable function rather than spread across a router.
"""

import asyncio
import time
from dataclasses import dataclass

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import LLMUnavailableError
from app.services.embeddings import Embedder
from app.services.llm import LLMClient
from app.services.prompts import (
    NO_ANSWER,
    SYSTEM_PROMPT,
    build_user_message,
    fit_context,
    render_context,
)
from app.services.retrieval import RetrievedPaper, retrieve


@dataclass(frozen=True)
class AnswerResult:
    """`degraded` is the router's cue to return 503 — the service does not know about HTTP."""

    answer: str
    sources: list[RetrievedPaper]
    degraded: bool = False


async def answer_question(
    session: AsyncSession, question: str, embedder: Embedder, llm: LLMClient
) -> AnswerResult:
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
        return AnswerResult(answer=NO_ANSWER, sources=[])

    # Sources come from the papers that fit, not from everything that cleared the threshold
    # -- otherwise a large top-k would cite context the model never saw.
    used = fit_context(relevant, max_chars=settings.max_context_chars)
    context = render_context(used)

    began = time.perf_counter()
    try:
        answer = await llm.complete(SYSTEM_PROMPT, build_user_message(question, context))
    except LLMUnavailableError as exc:
        # Retrieval worked. Returning what we found beats a 500 that throws it away.
        logger.warning("ask degraded after retrieval: {}", exc)
        return AnswerResult(
            answer=f"Could not generate an answer — {exc}. "
            "The most relevant papers are listed in sources.",
            sources=used,
            degraded=True,
        )

    logger.info(
        "ask answered: chars={} best={:.3f} sources={} llm={:.0f}ms",
        len(question),
        used[0].score,
        len(used),
        (time.perf_counter() - began) * 1000,
    )
    return AnswerResult(answer=answer, sources=used)
