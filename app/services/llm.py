"""The LLM client, behind a Protocol so tests never reach the network (NFR-4)."""

from typing import Protocol

import httpx
from loguru import logger

from app.core.config import settings
from app.core.errors import LLMUnavailableError


class LLMClient(Protocol):
    """The seam. `/ask` depends on this, never on httpx."""

    async def complete(self, system: str, user: str) -> str: ...


class OpenAICompatibleClient:
    """Groq by default, but any OpenAI-compatible `/chat/completions` endpoint (PRD §6.3)."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        """One pooled client for the process. Building one per request pays a fresh TLS
        handshake every time, which is pure latency against a host we always reuse."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=settings.llm_timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def configured(self) -> bool:
        """No key means degrade to retrieval-only, never a 500 halfway through."""
        return bool(settings.llm_api_key)

    async def complete(self, system: str, user: str) -> str:
        if not self.configured:
            raise LLMUnavailableError("no API key configured")

        try:
            response = await self._http().post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={
                    "model": settings.llm_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,  # grounded extraction, not creative writing
                },
            )
        except httpx.RequestError as exc:
            # The message, never the exception: a base URL can carry a key in the query.
            logger.error("llm request failed: {}", type(exc).__name__)
            raise LLMUnavailableError("could not reach the language model") from exc

        if response.status_code != 200:
            logger.error("llm returned {} for model {}", response.status_code, settings.llm_model)
            raise LLMUnavailableError(_describe(response.status_code))

        try:
            return response.json()["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, ValueError) as exc:
            logger.error("unexpected llm response shape")
            raise LLMUnavailableError("the language model returned an unexpected response") from exc


def _describe(status: int) -> str:
    """Actionable without echoing the provider's body, which can include the request."""
    if status in (401, 403):
        return "the language model rejected the API key"
    if status == 404:
        return f"the configured model '{settings.llm_model}' was not found"
    if status == 429:
        return "the language model is rate limiting; try again shortly"
    return "the language model is unavailable"
