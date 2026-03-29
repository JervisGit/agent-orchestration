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


@dataclass
class PolicyRule:
    """A single policy rule."""

    name: str
    stage: PolicyStage
    action: PolicyAction = PolicyAction.BLOCK
    provider: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicySet:
    """A collection of policy rules for a workflow."""

    policies: list[PolicyRule] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, yaml_str: str) -> "PolicySet":
        data = yaml.safe_load(yaml_str)
        rules = []
        for p in data.get("policies", []):
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

    def get_rules(self, stage: PolicyStage) -> list[PolicyRule]:
        return [p for p in self.policies if p.stage == stage]