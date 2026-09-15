"""Request and response shapes for `POST /ask`."""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.core.config import settings

# Stripped then length-checked, so "   " is a 422 rather than an embedding call on nothing.
QuestionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=settings.max_question_chars),
]


class Question(BaseModel):
    question: QuestionText = Field(examples=["How do LLM agents reuse past experience?"])


class Source(BaseModel):
    """A paper that was passed to the model as context -- never a paper that merely ranked."""

    arxiv_id: str = Field(examples=["2509.01234"])
    title: str
    score: float = Field(description="Cosine similarity to the question.")


class Answer(BaseModel):
    answer: str
    sources: list[Source]
