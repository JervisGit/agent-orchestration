"""Ollama LLM provider — local LLM inference via Ollama."""

import logging
from typing import Any

import httpx

from ao.llm.base import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    """LLM provider wrapping a local Ollama instance.

    Uses the Ollama OpenAI-compatible API (/v1/chat/completions).
    Works with any model pulled locally: llama3, mistral, phi3, etc.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        default_model: str = "llama3.2",
    ):
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        model_name = model or self._default_model
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self._base_url}/api/chat",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        content = data.get("message", {}).get("content", "")
        usage = {}
        if "prompt_eval_count" in data:
            usage["prompt_tokens"] = data["prompt_eval_count"]
        if "eval_count" in data:
            usage["completion_tokens"] = data["eval_count"]
            usage["total_tokens"] = usage.get("prompt_tokens", 0) + data["eval_count"]

        return LLMResponse(
            content=content,
            model=data.get("model", model_name),
            usage=usage,
            raw=data,
        )
