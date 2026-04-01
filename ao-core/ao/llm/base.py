"""Abstract LLM provider interface."""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResponse:
    """Standardized response from any LLM provider."""

    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)  # prompt_tokens, completion_tokens, total_tokens
    raw: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict] | None = None  # populated when LLM invokes tools


class LLMProvider(ABC):
    """Base class for LLM provider wrappers."""

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a completion request and return a standardized response."""

    async def complete_stream(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Stream completion tokens.  Default falls back to complete() and yields once."""
        resp = await self.complete(
            messages, model=model, temperature=temperature,
            max_tokens=max_tokens, **kwargs
        )
        yield resp.content
