"""Application manifest — config-driven app onboarding.

Each DSAI app registers with AO via a YAML manifest that declares:
- App identity (service principal, identity mode)
- Agents with system prompts and tool access
- Tools (with connection config)
- Policies to apply
- Observability project mapping

This allows app teams to onboard without code changes to AO.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ao.identity.context import IdentityMode


@dataclass
class ToolConfig:
    """Configuration for a tool available to agents."""

    name: str
    type: str  # "api", "database", "search_index", "adls", "custom"
    description: str = ""
    endpoint: str | None = None  # API URL or connection ref
    connection_secret: str | None = None  # Key Vault secret name
    identity_mode: str | None = None  # Override: "user_delegated" or "service"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentConfig:
    """Configuration for an agent within an app."""

    name: str
    system_prompt: str = ""
    model: str = "gpt-4o"
    tools: list[str] = field(default_factory=list)  # References to tool names
    temperature: float = 0.0
    max_tokens: int | None = None
    # Standard Operating Procedure injected into this agent's system prompt
    sop: str = ""
    # Python expression evaluated against state after the agent runs.
    # If truthy, sets state["hitl_required"] = True.
    # Namespace: {"state": state, "taxpayer": state.get("taxpayer"), "output": state.get("output")}
    # Example: "taxpayer and taxpayer.get('penalty_count', 0) >= 3"
    hitl_condition: str | None = None
    # Arbitrary key/value pairs attached to this agent's Langfuse generation as metadata
    trace_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppManifest:
    """Full manifest for a DSAI application."""

    app_id: str
    display_name: str
    description: str = ""

    # Workflow pattern: "router" | "linear" | "supervisor" | "planner" | "magentic"
    pattern: str = "router"
    # Name of the agent that classifies/routes (used by ManifestExecutor for router/magentic)
    classifier_agent: str = "classifier"
    # Agents eligible for multi-intent dispatch (magentic pattern only).
    # If empty, all non-classifier agents are candidates.
    intent_agents: list[str] = field(default_factory=list)

    # Identity
    identity_mode: IdentityMode = IdentityMode.SERVICE
    service_principal_id: str | None = None  # Entra app registration client ID

    # Agents
    agents: list[AgentConfig] = field(default_factory=list)

    # Tools
    tools: list[ToolConfig] = field(default_factory=list)

    # Policies (inline or reference to policy file)
    policies_file: str | None = None
    policies_inline: dict[str, Any] | None = None

    # Observability
    langfuse_project: str | None = None  # Langfuse project for trace isolation

    # LLM
    llm_endpoint: str | None = None  # Azure OpenAI endpoint (shared or app-specific)
    llm_api_key_secret: str | None = None  # Key Vault secret name

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AppManifest":
        """Load an app manifest from a YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)

        agents = [
            AgentConfig(**a) for a in data.get("agents", [])
        ]
        tools = [
            ToolConfig(**t) for t in data.get("tools", [])
        ]

        return cls(
            app_id=data["app_id"],
            display_name=data.get("display_name", data["app_id"]),
            description=data.get("description", ""),
            pattern=data.get("pattern", "router"),
            classifier_agent=data.get("classifier_agent", "classifier"),
            identity_mode=IdentityMode(data.get("identity_mode", "service")),
            service_principal_id=data.get("service_principal_id"),
            agents=agents,
            tools=tools,
            policies_file=data.get("policies_file"),
            policies_inline=data.get("policies"),
            langfuse_project=data.get("langfuse_project"),
            llm_endpoint=data.get("llm_endpoint"),
            llm_api_key_secret=data.get("llm_api_key_secret"),
            intent_agents=data.get("intent_agents", []),
        )

    @classmethod
    def from_yaml_string(cls, yaml_str: str) -> "AppManifest":
        """Load an app manifest from a YAML string."""
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_str)
            f.flush()
            return cls.from_yaml(f.name)
