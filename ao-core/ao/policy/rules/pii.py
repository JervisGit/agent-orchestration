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
    """Detect PII in text. If action is 'redact', replace PII with [REDACTED].

    Each field ("input", "output") is scanned and redacted independently so
    that redacting input never overwrites the output field and vice-versa.
    """
    found: list[str] = []

    for field in ("input", "output"):
        field_text = data.get(field)
        if not isinstance(field_text, str) or not field_text:
            continue
        field_redacted = field_text
        for pii_type, pattern in _PII_PATTERNS.items():
            if pattern.search(field_text):
                if pii_type not in found:
                    found.append(pii_type)
                if rule.action == PolicyAction.REDACT:
                    field_redacted = pattern.sub(f"[{pii_type.upper()}_REDACTED]", field_redacted)
        if rule.action == PolicyAction.REDACT and field_redacted != field_text:
            data[field] = field_redacted

    if found:
        return PolicyResult(
            rule_name=rule.name,
            passed=rule.action == PolicyAction.REDACT,  # Redact passes (modified), warn does not
            action=rule.action,
            detail=f"PII detected: {', '.join(found)}",
        )

    return PolicyResult(rule_name=rule.name, passed=True, action=rule.action)