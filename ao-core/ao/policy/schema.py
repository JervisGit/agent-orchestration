"""Policy YAML schema definitions."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import yaml


class PolicyStage(Enum):
    PRE_EXECUTION = "pre_execution"
    POST_EXECUTION = "post_execution"
    RUNTIME = "runtime"


class PolicyAction(Enum):
    BLOCK = "block"
    REDACT = "redact"
    WARN = "warn"
    LOG = "log"


# Built-in rule names for validation
BUILT_IN_RULES = frozenset({
    "content_safety",
    "pii_filter",
    "token_budget",
    "rate_limit",
    "allowed_actions",
})


@dataclass
class PolicyRule:
    """A single policy rule."""

    name: str
    stage: PolicyStage
    action: PolicyAction = PolicyAction.BLOCK
    provider: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.name or not self.name.strip():
            raise ValueError("PolicyRule name cannot be empty")


@dataclass
class PolicySet:
    """A collection of policy rules for a workflow."""

    policies: list[PolicyRule] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, yaml_str: str) -> "PolicySet":
        data = yaml.safe_load(yaml_str)
        if not isinstance(data, dict):
            raise ValueError("Policy YAML must be a mapping with a 'policies' key")
        rules = []
        for p in data.get("policies", []):
            if "name" not in p or "stage" not in p:
                raise ValueError(f"Each policy must have 'name' and 'stage': {p}")
            rules.append(
                PolicyRule(
                    name=p["name"],
                    stage=PolicyStage(p["stage"]),
                    action=PolicyAction(p.get("action", "block")),
                    provider=p.get("provider"),
                    params={
                        k: v
                        for k, v in p.items()
                        if k not in ("name", "stage", "action", "provider")
                    },
                )
            )
        return cls(policies=rules)

    @classmethod
    def from_yaml_file(cls, path: str) -> "PolicySet":
        """Load a PolicySet from a YAML file path."""
        with open(path) as f:
            return cls.from_yaml(f.read())

    def get_rules(self, stage: PolicyStage) -> list[PolicyRule]:
        return [p for p in self.policies if p.stage == stage]

    def validate(self) -> list[str]:
        """Return a list of validation warnings (e.g. unknown rule names)."""
        warnings = []
        for rule in self.policies:
            if rule.name not in BUILT_IN_RULES and not rule.provider:
                warnings.append(
                    f"Rule '{rule.name}' is not built-in and has no provider. "
                    "Register a custom handler or set a provider."
                )
        return warnings