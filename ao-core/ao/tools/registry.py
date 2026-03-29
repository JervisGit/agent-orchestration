"""Tool registry — registration and discovery of available tools.

Tools are registered with metadata (name, description, required identity mode)
and can be discovered by agents at runtime.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from ao.identity.context import IdentityMode

logger = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    """Specification for a registered tool."""

    name: str
    description: str
    fn: Callable[..., Any]
    required_identity: IdentityMode | None = None  # None = any identity
    parameters: dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    """Central registry for tools available to agents."""

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        description: str = "",
        required_identity: IdentityMode | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        """Register a tool."""
        self._tools[name] = ToolSpec(
            name=name,
            description=description,
            fn=fn,
            required_identity=required_identity,
            parameters=parameters or {},
        )
        logger.info("Registered tool: %s", name)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def to_langchain_tools(self) -> list:
        """Convert registered tools to LangChain tool format."""
        from langchain_core.tools import StructuredTool

        lc_tools = []
        for spec in self._tools.values():
            lc_tools.append(
                StructuredTool.from_function(
                    func=spec.fn,
                    name=spec.name,
                    description=spec.description,
                )
            )
        return lc_tools