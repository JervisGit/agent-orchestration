"""Pydantic schemas for tool definitions and inter-agent messages.

These models enforce type safety at two key boundaries:

- ``ToolSchema``: validates a tool definition when it is registered with
  ``ManifestExecutor.register_tool()``.  Malformed schemas (bad name, empty
  description, wrong parameter structure) raise immediately at registration
  time rather than silently corrupting the OpenAI function-calling payload.

- ``AgentMessage``: typed wrapper for messages passed between LangGraph nodes
  (classifier decisions, specialist replies, supervisor routing, tool results).
  Using ``AgentMessage`` to construct node output messages catches shape
  mismatches at node boundaries — the message is validated, then converted to
  a plain dict via ``.to_dict()`` for storage in LangGraph state.

- ``ToolResult``: typed result from a single tool-call execution.  Created
  inside ``ManifestExecutor._execute_tool_call()`` to validate the content
  string and optional state update dict before they are merged into state.
"""

from typing import Any

from pydantic import BaseModel, field_validator


class ToolParameterSchema(BaseModel):
    """JSON Schema for a tool's parameters (OpenAI function-calling format)."""

    type: str = "object"
    properties: dict[str, Any] = {}
    required: list[str] = []

    model_config = {"extra": "allow"}


class ToolSchema(BaseModel):
    """Validated definition for a tool registered with ManifestExecutor.

    Equivalent to the OpenAI ``function`` schema object but validated at
    registration time so errors surface immediately.

    Example::

        schema = {
            "name": "lookup_taxpayer",
            "description": "Look up taxpayer record from the database by TIN.",
            "parameters": {
                "type": "object",
                "properties": {"tin": {"type": "string", "description": "Taxpayer TIN"}},
                "required": ["tin"],
            },
        }
        ToolSchema.model_validate(schema)  # raises ValidationError if malformed
    """

    name: str
    description: str
    parameters: ToolParameterSchema = ToolParameterSchema()

    @field_validator("name")
    @classmethod
    def name_is_valid_identifier(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c == "_" for c in v):
            raise ValueError(
                f"Tool name must contain only alphanumeric characters and underscores, got: {v!r}"
            )
        return v

    @field_validator("description")
    @classmethod
    def description_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Tool description must not be empty")
        return v

    def to_openai_function(self) -> dict[str, Any]:
        """Return the dict in OpenAI function-calling format."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters.model_dump(),
        }


class ToolResult(BaseModel):
    """Typed result from executing a registered tool during a specialist's tool-calling loop."""

    tool_name: str
    call_id: str
    content: str
    state_update: dict[str, Any] = {}


class AgentMessage(BaseModel):
    """Typed message passed between LangGraph nodes.

    Used to validate inter-node communication at node boundaries.  After
    constructing and validating, call ``.to_dict()`` to get the plain dict
    that LangGraph state (``list[dict]``) expects.

    Roles:
    - ``"classifier"``      — single-intent routing decision
    - ``"intent_classifier"`` — multi-intent detection (concurrent pattern)
    - ``"agent"``           — specialist reply text
    - ``"supervisor"``      — supervisor routing decision or ``"FINISH"``
    - ``"merge"``           — merged output from concurrent specialists
    - ``"tool"``            — tool call result (role used in message history)
    """

    role: str
    content: str
    agent_name: str | None = None
    tool_result: ToolResult | None = None
    metadata: dict[str, Any] = {}

    def to_dict(self) -> dict[str, Any]:
        """Return minimal dict for LangGraph state (role + content only)."""
        return {"role": self.role, "content": self.content}
