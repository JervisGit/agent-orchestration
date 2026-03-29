"""Abstract orchestration engine interface."""

from abc import ABC, abstractmethod
from typing import Any


class OrchestrationEngine(ABC):
    """Base class for all orchestration engines."""

    @abstractmethod
    async def run(self, workflow_id: str, input_data: dict[str, Any]) -> dict[str, Any]:
        """Execute a workflow and return results."""

    @abstractmethod
    async def resume(self, workflow_id: str, checkpoint_id: str) -> dict[str, Any]:
        """Resume a workflow from a checkpoint."""
