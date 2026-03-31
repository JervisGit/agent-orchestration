"""OpenAI API LLM provider (api.openai.com)."""

import logging
from typing import Any

from openai import AsyncOpenAI

from ao.llm.base import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    """LLM provider wrapping the standard OpenAI API."""

    def __init__(
        self,
        api_key: str,
        default_model: str = "gpt-4.1-mini",
        base_url: str | None = None,
    ):
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)
        self._default_model = default_model

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        target_model = model or self._default_model
        call_kwargs: dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            call_kwargs["max_tokens"] = max_tokens
        call_kwargs.update(kwargs)

        response = await self._client.chat.completions.create(**call_kwargs)
        choice = response.choices[0]
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return LLMResponse(
            content=choice.message.content or "",
            model=response.model or target_model,
            usage=usage,
            raw=response.model_dump(),
        )
