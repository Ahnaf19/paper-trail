"""POST /ask. Validates the question, delegates, and maps the outcome to a status code."""

from fastapi import APIRouter, Response, status

from app.dependencies import EmbedderDep, LLMDep, SessionDep
from app.schemas.ask import Answer, Question, Source
from app.services import qa

router = APIRouter(tags=["ask"])


@router.post("/ask", summary="Answer a question from the stored abstracts")
async def ask(
    payload: Question,
    session: SessionDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    response: Response,
) -> Answer:
    result = await qa.answer_question(session, payload.question, embedder, llm)
    if result.degraded:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Answer(
        answer=result.answer,
        sources=[
            Source(arxiv_id=p.arxiv_id, title=p.title, score=round(p.score, 4))
            for p in result.sources
        ],
    )
