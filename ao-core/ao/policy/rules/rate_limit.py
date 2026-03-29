"""Rate limiting rule — enforce per-workflow call frequency limits."""

import time
from collections import defaultdict

from ao.policy.engine import PolicyResult
from ao.policy.schema import PolicyRule

# In-memory rate tracker. Production: use Redis INCR + EXPIRE.
_call_counts: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(data: dict, rule: PolicyRule) -> PolicyResult:
    """Check if the workflow has exceeded the allowed call rate.

    Params (from policy YAML):
        max_calls_per_minute: int (default 60)
    """
    max_calls = rule.params.get("max_calls_per_minute", 60)
    window = 60.0  # seconds
    workflow_id = data.get("workflow_id", "default")
    now = time.monotonic()

    # Prune old entries
    timestamps = _call_counts[workflow_id]
    _call_counts[workflow_id] = [t for t in timestamps if now - t < window]
    _call_counts[workflow_id].append(now)

    current = len(_call_counts[workflow_id])
    if current > max_calls:
        return PolicyResult(
            rule_name=rule.name,
            passed=False,
            action=rule.action,
            detail=f"Rate limit exceeded: {current}/{max_calls} calls/min",
        )
    return PolicyResult(rule_name=rule.name, passed=True, action=rule.action)
