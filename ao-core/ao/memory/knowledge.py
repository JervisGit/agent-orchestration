"""Knowledge retrieval — abstract RAG interface.

Each app provides its own knowledge source (Azure AI Search, pgvector, etc).
This module defines the abstract interface and a simple in-memory implementation
for testing.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeResult:
    """A single retrieval result."""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


class KnowledgeSource(ABC):
    """Abstract interface for knowledge retrieval."""

    @abstractmethod
    async def search(
        self, query: str, top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[KnowledgeResult]:
        """Search the knowledge source and return ranked results."""


class InMemoryKnowledgeSource(KnowledgeSource):
    """Simple in-memory knowledge source for testing/demo."""

    def __init__(self):
        self._documents: list[dict[str, Any]] = []

    def add_document(self, content: str, metadata: dict[str, Any] | None = None) -> None:
        self._documents.append({"content": content, "metadata": metadata or {}})

    async def search(
        self, query: str, top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[KnowledgeResult]:
        """Simple substring match (for demo only — production uses vector search)."""
        results = []
        query_lower = query.lower()
        for doc in self._documents:
            content = doc["content"]
            if query_lower in content.lower():
                results.append(
                    KnowledgeResult(content=content, metadata=doc["metadata"], score=1.0)
                )
        return results[:top_k]
