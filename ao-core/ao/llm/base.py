"""Abstract LLM provider interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResponse:
    """Standardized response from any LLM provider."""

    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)  # prompt_tokens, completion_tokens, total_tokens
    raw: dict[str, Any] = field(default_factory=dict)


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
