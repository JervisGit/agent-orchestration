"""Allowed-actions whitelist rule — restrict which tools/actions an agent may invoke."""

from ao.policy.engine import PolicyResult
from ao.policy.schema import PolicyRule


def check_allowed_actions(data: dict, rule: PolicyRule) -> PolicyResult:
    """Check that the requested action is in the whitelist.

    Params (from policy YAML):
        allowed: list[str] — permitted tool/action names
    """
    allowed = set(rule.params.get("allowed", []))
    requested = data.get("action", "") or data.get("tool_name", "")

    if not allowed:
        # No whitelist configured → allow everything
        return PolicyResult(rule_name=rule.name, passed=True, action=rule.action)

    if requested and requested not in allowed:
        return PolicyResult(
            rule_name=rule.name,
            passed=False,
            action=rule.action,
            detail=f"Action '{requested}' not in allowed list: {sorted(allowed)}",
        )
    return PolicyResult(rule_name=rule.name, passed=True, action=rule.action)
