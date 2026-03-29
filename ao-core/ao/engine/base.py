"""Abstract orchestration engine interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ao.identity.context import IdentityContext
from ao.policy.schema import PolicySet


@dataclass
class WorkflowConfig:
    """Configuration for a workflow run."""

    workflow_id: str
    identity: IdentityContext | None = None
    policies: PolicySet | None = None
    hitl_enabled: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowResult:
    """Result of a workflow execution."""

    workflow_id: str
    status: str  # "completed", "failed", "pending_approval"
    output: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class OrchestrationEngine(ABC):
    """Base class for all orchestration engines."""

    @abstractmethod
    async def run(
        self, config: WorkflowConfig, input_data: dict[str, Any]
    ) -> WorkflowResult:
        """Execute a workflow and return results."""

    @abstractmethod
    async def resume(
        self, workflow_id: str, checkpoint_id: str
    ) -> WorkflowResult:
        """Resume a workflow from a checkpoint."""
