"""Identity-scoped tool execution.

Ensures tools are only executed if the current identity context has the
required permission level.
"""

import logging
from typing import Any

from ao.identity.context import IdentityContext
from ao.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Executes tools with identity validation."""

    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    async def execute(
        self,
        tool_name: str,
        identity: IdentityContext,
        args: dict[str, Any] | None = None,
    ) -> Any:
        spec = self._registry.get(tool_name)
        if not spec:
            raise ValueError(f"Tool '{tool_name}' not found in registry")

        # Validate identity requirement
        if spec.required_identity and identity.mode.value != spec.required_identity.value:
            raise PermissionError(
                f"Tool '{tool_name}' requires {spec.required_identity.value} "
                f"identity, but got {identity.mode.value}"
            )

        logger.info(
            "Executing tool '%s' with identity mode=%s",
            tool_name,
            identity.mode.value,
        )

        import asyncio

        if asyncio.iscoroutinefunction(spec.fn):
            return await spec.fn(**(args or {}))
        return spec.fn(**(args or {}))