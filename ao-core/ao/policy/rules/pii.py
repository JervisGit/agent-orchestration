"""PII detection and redaction rule.

A simple regex-based PII detector for demo/dev. Production should use
Azure AI Language PII detection or Presidio.
"""

import re

from ao.policy.engine import PolicyResult
from ao.policy.schema import PolicyAction, PolicyRule

_PII_PATTERNS = {
    "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "phone": re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"),
    "nric": re.compile(r"\b[STFG]\d{7}[A-Z]\b", re.IGNORECASE),  # Singapore NRIC
}


def check_pii(data: dict, rule: PolicyRule) -> PolicyResult:
    """Detect PII in text. If action is 'redact', replace PII with [REDACTED]."""
    text = data.get("input", "") or data.get("output", "")
    if not isinstance(text, str):
        text = str(text)

    found: list[str] = []
    redacted = text
    for pii_type, pattern in _PII_PATTERNS.items():
        if pattern.search(text):
            found.append(pii_type)
            if rule.action == PolicyAction.REDACT:
                redacted = pattern.sub(f"[{pii_type.upper()}_REDACTED]", redacted)

    if found:
        # Update the data in-place if redacting
        if rule.action == PolicyAction.REDACT:
            if "input" in data and data["input"]:
                data["input"] = redacted
            if "output" in data and data["output"]:
                data["output"] = redacted

        return PolicyResult(
            rule_name=rule.name,
            passed=rule.action == PolicyAction.REDACT,  # Redact passes (modified), block doesn't
            action=rule.action,
            detail=f"PII detected: {', '.join(found)}",
        )

    return PolicyResult(rule_name=rule.name, passed=True, action=rule.action)