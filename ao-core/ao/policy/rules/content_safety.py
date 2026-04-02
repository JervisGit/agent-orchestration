"""Content safety rule — keyword/regex filter for Phase 1.

Catches common jailbreak patterns, toxicity signals, and bias bait.
For production, replace with Azure AI Content Safety + Prompt Shield
(see ADR-012). The handler signature is unchanged so swapping providers
requires only replacing this file — the PolicyEngine and PolicySet YAML
need no modification.
"""

import re

from ao.policy.engine import PolicyResult
from ao.policy.schema import PolicyRule

# ── Jailbreak / Prompt Injection ──────────────────────────────────
# Patterns targeting role override, system-prompt extraction, and
# classic DAN / jailbreak phrasing.
_JAILBREAK_PATTERNS = [
    re.compile(r"\bignore\b.{0,30}\b(previous|prior|above|all)\b.{0,30}\b(instructions?|prompts?|rules?)\b", re.IGNORECASE),
    re.compile(r"\b(disregard|forget|override|bypass)\b.{0,30}\b(instructions?|system prompt|guidelines?|constraints?)\b", re.IGNORECASE),
    re.compile(r"\byou are now\b.{0,60}\b(DAN|jailbroken|unrestricted|without (restrictions?|rules?|guidelines?))\b", re.IGNORECASE),
    re.compile(r"\bdo anything now\b", re.IGNORECASE),
    re.compile(r"\bact as (if you (have no|are not)|an? (unrestricted|unfiltered|evil))\b", re.IGNORECASE),
    re.compile(r"\b(reveal|show|print|output|repeat)\b.{0,30}\b(system prompt|instructions?|initial prompt)\b", re.IGNORECASE),
    re.compile(r"\bpretend (you (are|have no|don.t have)|there (are|is) no)\b.{0,40}\b(rules?|guidelines?|restrictions?|filter)\b", re.IGNORECASE),
    re.compile(r"\[INST\]|\[\/INST\]|<\|system\|>|<\|user\|>|<\|assistant\|>", re.IGNORECASE),  # raw LLM control tokens
    re.compile(r"\btoken\s*smuggling\b|\bprompt\s*injection\b", re.IGNORECASE),
]

# ── Toxicity / Threats / Harassment ───────────────────────────────
_TOXICITY_PATTERNS = [
    re.compile(r"\b(kill|murder|harm|attack|bomb|shoot|stab)\b.{0,20}\b(you|them|him|her|staff|officer|agent)\b", re.IGNORECASE),
    re.compile(r"\bi (will|am going to|gonna) (kill|hurt|destroy|find)\b", re.IGNORECASE),
    re.compile(r"\b(you('re| are) (stupid|useless|idiots?|incompetent))\b", re.IGNORECASE),
    re.compile(r"\bgo (to hell|f\*ck yourself|die)\b", re.IGNORECASE),
    re.compile(r"\bf+u+c+k+\s*(you|off|this|the)\b", re.IGNORECASE),
]

# ── Bias Bait ─────────────────────────────────────────────────────
# Attempts to elicit discriminatory or politically charged responses
# from the agent in the context of tax/government services.
_BIAS_BAIT_PATTERNS = [
    re.compile(r"\b(tax (foreigners|immigrants|minorities|chinese|malays?|indians?|muslims?|christians?) (more|differently|less)\b)", re.IGNORECASE),
    re.compile(r"\bwhich (race|religion|nationality|ethnicity) pays? (less|more|no) tax\b", re.IGNORECASE),
    re.compile(r"\b(discriminate|favour|favor)\b.{0,30}\b(race|religion|gender|nationality)\b", re.IGNORECASE),
]

# Combined with category labels for structured reporting
_PATTERN_GROUPS: list[tuple[str, list[re.Pattern]]] = [
    ("jailbreak", _JAILBREAK_PATTERNS),
    ("toxicity", _TOXICITY_PATTERNS),
    ("bias_bait", _BIAS_BAIT_PATTERNS),
]


def check_content_safety(data: dict, rule: PolicyRule) -> PolicyResult:
    """Check input/output text for jailbreak, toxicity, and bias bait patterns.

    Returns a failed PolicyResult on the first match found, including the
    category and matched pattern for Langfuse trace annotation.
    """
    text = data.get("input", "") or data.get("output", "")
    if not isinstance(text, str):
        text = str(text)

    for category, patterns in _PATTERN_GROUPS:
        for pattern in patterns:
            if pattern.search(text):
                return PolicyResult(
                    rule_name=rule.name,
                    passed=False,
                    action=rule.action,
                    detail=f"Content safety violation [{category}]: matched pattern '{pattern.pattern[:60]}'",
                )

    return PolicyResult(rule_name=rule.name, passed=True, action=rule.action)
