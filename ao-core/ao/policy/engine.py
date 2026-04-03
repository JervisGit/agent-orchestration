"""Policy evaluation engine — loads policies and evaluates at pre/post/runtime stages."""

import logging
from dataclasses import dataclass, field
from typing import Any

from ao.policy.schema import PolicyAction, PolicyRule, PolicySet, PolicyStage

logger = logging.getLogger(__name__)


@dataclass
class PolicyResult:
    """Result of evaluating a single policy rule."""

    rule_name: str
    passed: bool
    action: PolicyAction
    detail: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class PolicyEvaluation:
    """Aggregated result of evaluating all rules for a stage."""

    allowed: bool
    results: list[PolicyResult]
    modified_data: dict[str, Any] | None = None  # If redaction was applied


class PolicyEngine:
    """Evaluates policy rules against input/output data."""

    def __init__(self):
        self._rule_handlers: dict[str, Any] = {}

    def register_rule(self, name: str, handler) -> None:
        """Register a callable rule handler: handler(data, rule) -> PolicyResult."""
        self._rule_handlers[name] = handler

    def register_builtin_rules(self) -> None:
        """Register all built-in rule handlers."""
        from ao.policy.rules.allowed_actions import check_allowed_actions
        from ao.policy.rules.content_safety import check_content_safety
        from ao.policy.rules.pii import check_pii
        from ao.policy.rules.rate_limit import check_rate_limit
        from ao.policy.rules.token_budget import check_token_budget

        self._rule_handlers["content_safety"] = check_content_safety
        self._rule_handlers["pii_filter"] = check_pii
        self._rule_handlers["token_budget"] = check_token_budget
        self._rule_handlers["rate_limit"] = check_rate_limit
        self._rule_handlers["allowed_actions"] = check_allowed_actions

    async def evaluate(
        self,
        stage: PolicyStage,
        policies: PolicySet,
        data: dict[str, Any],
    ) -> PolicyEvaluation:
        """Evaluate all rules for a given stage against the data."""
        rules = policies.get_rules(stage)
        results: list[PolicyResult] = []
        blocked = False
        modified_data = dict(data)

        for rule in rules:
            handler = self._rule_handlers.get(rule.name)
            if not handler:
                logger.warning("No handler registered for rule '%s', skipping", rule.name)
                continue

            result = await self._run_handler(handler, modified_data, rule)
            results.append(result)

            if not result.passed:
                if rule.action == PolicyAction.BLOCK:
                    blocked = True
                    logger.warning("Policy '%s' BLOCKED: %s", rule.name, result.detail)
                elif rule.action == PolicyAction.WARN:
                    logger.warning("Policy '%s' WARNING: %s", rule.name, result.detail)

        return PolicyEvaluation(
            allowed=not blocked,
            results=results,
            modified_data=modified_data,
        )

    async def _run_handler(
        self, handler, data: dict[str, Any], rule: PolicyRule
    ) -> PolicyResult:
        try:
            import asyncio

            if asyncio.iscoroutinefunction(handler):
                return await handler(data, rule)
            return handler(data, rule)
        except Exception as e:
            logger.exception("Rule handler '%s' raised an error", rule.name)
            return PolicyResult(
                rule_name=rule.name,
                passed=False,
                action=rule.action,
                detail=f"Handler error: {e}",
            )