"""Content safety rule — basic keyword-based filter.

For production, integrate with Azure AI Content Safety API.
This serves as a local-dev placeholder.
"""

import re

from ao.policy.engine import PolicyResult
from ao.policy.schema import PolicyRule

# Minimal placeholder patterns — production would use Azure AI Content Safety
_BLOCKED_PATTERNS = [
    re.compile(r"\b(ignore previous instructions)\b", re.IGNORECASE),
    re.compile(r"\b(system prompt)\b", re.IGNORECASE),
]


def check_content_safety(data: dict, rule: PolicyRule) -> PolicyResult:
    """Check input/output text for unsafe content patterns."""
    text = data.get("input", "") or data.get("output", "")
    if not isinstance(text, str):
        text = str(text)

    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(text):
            return PolicyResult(
                rule_name=rule.name,
                passed=False,
                action=rule.action,
                detail=f"Content safety violation detected: {pattern.pattern}",
            )

    return PolicyResult(
        rule_name=rule.name,
        passed=True,
        action=rule.action,
    )