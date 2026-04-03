"""Identity-scoped tool execution.

Ensures tools are only executed if the current identity context has the
required permission level.

Identity injection
------------------
After identity validation, execute() inspects the tool callable's signature.
If it declares an `identity` parameter, the IdentityContext is injected as a
kwarg so the tool can acquire a Bearer token via:

    async def my_tool(tin: str, identity: IdentityContext | None = None) -> dict:
        token = await credential_provider.get_token(identity, APIM_SCOPE)
        ...

Tools that do NOT declare `identity` are called unchanged — no impact on
existing tool implementations.
"""

import inspect
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

        call_args = dict(args or {})

        # Inject identity only if the tool explicitly declares the parameter —
        # keeps existing tools unchanged while opt-in tools get the context.
        sig = inspect.signature(spec.fn)
        if "identity" in sig.parameters:
            call_args["identity"] = identity

        import asyncio

        if asyncio.iscoroutinefunction(spec.fn):
            return await spec.fn(**call_args)
        return spec.fn(**call_args)