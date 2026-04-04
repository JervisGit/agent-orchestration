"""Azure OpenAI / Foundry LLM provider."""

import logging
from collections.abc import AsyncGenerator
from typing import Any

from openai import AsyncAzureOpenAI

from ao.llm.base import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


class AzureOpenAIProvider(LLMProvider):
    """LLM provider wrapping Azure OpenAI endpoints."""

    def __init__(
        self,
        endpoint: str,
        api_key: str | None = None,
        api_version: str = "2025-01-01-preview",
        default_model: str = "gpt-4o",
        azure_ad_token_provider=None,
    ):
        kwargs: dict[str, Any] = {
            "azure_endpoint": endpoint,
            "api_version": api_version,
        }
        if azure_ad_token_provider:
            kwargs["azure_ad_token_provider"] = azure_ad_token_provider
        elif api_key:
            kwargs["api_key"] = api_key
        else:
            raise ValueError("Provide either api_key or azure_ad_token_provider")

        self._client = AsyncAzureOpenAI(**kwargs)
        self._default_model = default_model

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        deployment = model or self._default_model
        call_kwargs: dict[str, Any] = {
            "model": deployment,
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

        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in choice.message.tool_calls
            ]

        return LLMResponse(
            content=choice.message.content or "",
            model=response.model or deployment,
            usage=usage,
            raw=response.model_dump(),
            tool_calls=tool_calls,
        )

    async def complete_stream(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Stream completion tokens one chunk at a time."""
        deployment = model or self._default_model
        call_kwargs: dict[str, Any] = {
            "model": deployment,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            call_kwargs["max_tokens"] = max_tokens
        call_kwargs.update(kwargs)

        stream = await self._client.chat.completions.create(**call_kwargs)
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def __init__(
        self,
        endpoint: str,
        api_key: str | None = None,
        api_version: str = "2025-01-01-preview",
        default_model: str = "gpt-4o",
        azure_ad_token_provider=None,
    ):
        kwargs: dict[str, Any] = {
            "azure_endpoint": endpoint,
            "api_version": api_version,
        }
        if azure_ad_token_provider:
            kwargs["azure_ad_token_provider"] = azure_ad_token_provider
        elif api_key:
            kwargs["api_key"] = api_key
        else:
            raise ValueError("Provide either api_key or azure_ad_token_provider")

        self._client = AsyncAzureOpenAI(**kwargs)
        self._default_model = default_model

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        deployment = model or self._default_model
        call_kwargs: dict[str, Any] = {
            "model": deployment,
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

        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in choice.message.tool_calls
            ]

        return LLMResponse(
            content=choice.message.content or "",
            model=response.model or deployment,
            usage=usage,
            raw=response.model_dump(),
            tool_calls=tool_calls,
        )