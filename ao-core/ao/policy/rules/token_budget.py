"""Token budget enforcement rule."""

from ao.policy.engine import PolicyResult
from ao.policy.schema import PolicyRule


def check_token_budget(data: dict, rule: PolicyRule) -> PolicyResult:
    """Check if token usage is within the allowed budget."""
    max_tokens = rule.params.get("max_tokens_per_run", 50_000)
    current_tokens = data.get("total_tokens_used", 0)

    if current_tokens > max_tokens:
        return PolicyResult(
            rule_name=rule.name,
            passed=False,
            action=rule.action,
            detail=f"Token budget exceeded: {current_tokens}/{max_tokens}",
        )
    return PolicyResult(
        rule_name=rule.name,
        passed=True,
        action=rule.action,
    )