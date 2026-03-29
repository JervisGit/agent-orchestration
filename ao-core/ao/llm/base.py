"""Abstract LLM provider interface."""

from abc import ABC, abstractmethod
from typing import Any


class LLMProvider(ABC):
    """Base class for LLM provider wrappers."""

    @abstractmethod
    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        """Send a completion request and return the response with token usage."""
