"""Graceful degradation — fallback handlers for non-critical failures.

When a workflow step fails and is marked as non-critical, the fallback
handler provides a default response so the workflow can continue.
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class FallbackConfig:
    """Configuration for fallback behavior on a step."""

    enabled: bool = True
    default_output: dict[str, Any] | None = None
    handler: Callable[..., dict[str, Any]] | None = None  # Custom fallback fn


class FallbackHandler:
    """Manages fallback behavior for workflow steps."""

    def __init__(self):
        self._configs: dict[str, FallbackConfig] = {}

    def register(self, step_name: str, config: FallbackConfig) -> None:
        """Register fallback config for a step."""
        self._configs[step_name] = config

    def has_fallback(self, step_name: str) -> bool:
        config = self._configs.get(step_name)
        return config is not None and config.enabled

    def get_fallback_output(
        self, step_name: str, error: Exception, state: dict[str, Any]
    ) -> dict[str, Any]:
        """Get fallback output for a failed step.

        Args:
            step_name: The step that failed.
            error: The exception that was raised.
            state: Current workflow state.

        Returns:
            A partial state update to continue the workflow.
        """
        config = self._configs.get(step_name)
        if not config or not config.enabled:
            raise error

        logger.warning(
            "Step '%s' failed, using fallback: %s", step_name, error
        )

        if config.handler:
            return config.handler(step_name, error, state)

        if config.default_output:
            return config.default_output

        # Generic fallback
        return {
            "_fallback": True,
            "_fallback_step": step_name,
            "_fallback_error": str(error),
        }
