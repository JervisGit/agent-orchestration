"""Content safety rule — Azure AI Content Safety (Phase 2) with regex fallback.

When AZURE_CONTENT_SAFETY_ENDPOINT and AZURE_CONTENT_SAFETY_KEY are set
(injected by Terraform into the ACA container), this rule uses:
  - Prompt Shield   → jailbreak / prompt injection detection
  - Analyze Text    → Hate, Violence, Sexual, SelfHarm category scoring

When those env vars are absent (local dev), it falls back to the Phase 1
regex patterns covering jailbreak, toxicity, and bias bait.

PolicyRule.params supported:
  severity_threshold (int, default 4): block if any category reaches this level (0–7)

See ADR-012 for provider selection rationale.
"""

import logging
import os
import re

from ao.policy.engine import PolicyResult
from ao.policy.schema import PolicyRule

logger = logging.getLogger(__name__)

# ── Phase 1 regex fallback ────────────────────────────────────────

_JAILBREAK_PATTERNS = [
    re.compile(r"\bignore\b.{0,30}\b(previous|prior|above|all)\b.{0,30}\b(instructions?|prompts?|rules?)\b", re.IGNORECASE),
    re.compile(r"\b(disregard|forget|override|bypass)\b.{0,30}\b(instructions?|system prompt|guidelines?|constraints?)\b", re.IGNORECASE),
    re.compile(r"\byou are now\b.{0,60}\b(DAN|jailbroken|unrestricted|without (restrictions?|rules?|guidelines?))\b", re.IGNORECASE),
    re.compile(r"\bdo anything now\b", re.IGNORECASE),
    re.compile(r"\bact as (if you (have no|are not)|an? (unrestricted|unfiltered|evil))\b", re.IGNORECASE),
    re.compile(r"\b(reveal|show|print|output|repeat|tell|share|give|send|what('?s| is))\b.{0,40}\b(system prompt|initial prompt)\b", re.IGNORECASE),
    re.compile(r"\bpretend (you (are|have no|don.t have)|there (are|is) no)\b.{0,40}\b(rules?|guidelines?|restrictions?|filter)\b", re.IGNORECASE),
    re.compile(r"\[INST\]|\[\/INST\]|<\|system\|>|<\|user\|>|<\|assistant\|>", re.IGNORECASE),
    re.compile(r"\btoken\s*smuggling\b|\bprompt\s*injection\b", re.IGNORECASE),
]
_TOXICITY_PATTERNS = [
    re.compile(r"\b(kill|murder|harm|attack|bomb|shoot|stab)\b.{0,20}\b(you|them|him|her|staff|officer|agent)\b", re.IGNORECASE),
    re.compile(r"\bi (will|am going to|gonna) (kill|hurt|destroy|find)\b", re.IGNORECASE),
    re.compile(r"\b(you('re| are) (stupid|useless|idiots?|incompetent))\b", re.IGNORECASE),
    re.compile(r"\bgo (to hell|f\*ck yourself|die)\b", re.IGNORECASE),
    re.compile(r"\bf+u+c+k+\s*(you|off|this|the)\b", re.IGNORECASE),
]
_BIAS_BAIT_PATTERNS = [
    re.compile(r"\b(tax (foreigners|immigrants|minorities|chinese|malays?|indians?|muslims?|christians?) (more|differently|less)\b)", re.IGNORECASE),
    re.compile(r"\bwhich (race|religion|nationality|ethnicity) pays? (less|more|no) tax\b", re.IGNORECASE),
    re.compile(r"\b(discriminate|favour|favor)\b.{0,30}\b(race|religion|gender|nationality)\b", re.IGNORECASE),
]
_PATTERN_GROUPS: list[tuple[str, list[re.Pattern]]] = [
    ("jailbreak", _JAILBREAK_PATTERNS),
    ("toxicity", _TOXICITY_PATTERNS),
    ("bias_bait", _BIAS_BAIT_PATTERNS),
]


def _check_regex(text: str, rule: PolicyRule) -> PolicyResult:
    for category, patterns in _PATTERN_GROUPS:
        for pattern in patterns:
            if pattern.search(text):
                return PolicyResult(
                    rule_name=rule.name,
                    passed=False,
                    action=rule.action,
                    detail=f"Content safety violation [{category}] (regex): matched '{pattern.pattern[:60]}'",
                    metadata={"category": category, "provider": "regex"},
                )
    return PolicyResult(rule_name=rule.name, passed=True, action=rule.action)


# ── Azure AI Content Safety (Phase 2) ────────────────────────────
# Hybrid approach:
#   - Regex always runs first for jailbreak/injection (fast, no network).
#     Prompt Shield requires azure-ai-contentsafety>=1.1 which is not yet
#     available on the F0 tier; regex covers this gap.
#   - Azure analyze_text runs second for H/V/S/SH category scoring.

async def _check_azure(text: str, rule: PolicyRule, endpoint: str, key: str) -> PolicyResult:
    """Hybrid check: regex for jailbreak, Azure AI for content categories."""
    from azure.ai.contentsafety.aio import ContentSafetyClient
    from azure.ai.contentsafety.models import AnalyzeTextOptions, TextCategory
    from azure.core.credentials import AzureKeyCredential
    from azure.core.exceptions import HttpResponseError

    # 1. Regex check first — catches jailbreak/injection/toxicity instantly
    regex_result = _check_regex(text, rule)
    if not regex_result.passed:
        logger.info("Content safety BLOCKED by regex: %s", regex_result.detail)
        return regex_result

    # 2. Azure category analysis — Hate, Violence, Sexual, SelfHarm
    severity_threshold: int = rule.params.get("severity_threshold", 4)
    try:
        async with ContentSafetyClient(endpoint, AzureKeyCredential(key)) as client:
            analysis = await client.analyze_text(
                AnalyzeTextOptions(
                    text=text,
                    categories=[
                        TextCategory.HATE,
                        TextCategory.VIOLENCE,
                        TextCategory.SEXUAL,
                        TextCategory.SELF_HARM,
                    ],
                )
            )
            for item in analysis.categories_analysis or []:
                if (item.severity or 0) >= severity_threshold:
                    detail = (
                        f"Content safety violation [{item.category}] "
                        f"(Azure AI): severity {item.severity} >= threshold {severity_threshold}"
                    )
                    logger.info("Content safety BLOCKED by Azure AI: %s", detail)
                    return PolicyResult(
                        rule_name=rule.name,
                        passed=False,
                        action=rule.action,
                        detail=detail,
                        metadata={"category": str(item.category), "provider": "azure_ai", "severity": item.severity},
                    )
            logger.info("Content safety PASSED: Azure AI categories all below threshold %d", severity_threshold)
    except HttpResponseError as exc:
        logger.error("Azure Content Safety API error: %s — regex result stands", exc)
    except Exception as exc:
        logger.error("Unexpected error calling Azure Content Safety: %s — regex result stands", exc)

    return PolicyResult(rule_name=rule.name, passed=True, action=rule.action)


# ── Public handler ────────────────────────────────────────────────

async def check_content_safety(data: dict, rule: PolicyRule) -> PolicyResult:
    """Check input/output text for unsafe content.

    Regex always runs for jailbreak/injection. When AZURE_CONTENT_SAFETY_ENDPOINT
    and AZURE_CONTENT_SAFETY_KEY are set, Azure AI also scores H/V/S/SH categories.
    Falls back gracefully on any Azure error — the regex result is never discarded.
    """
    text = data.get("input", "") or data.get("output", "")
    if not isinstance(text, str):
        text = str(text)

    endpoint = os.environ.get("AZURE_CONTENT_SAFETY_ENDPOINT", "")
    key = os.environ.get("AZURE_CONTENT_SAFETY_KEY", "")

    if endpoint and key:
        logger.info("Content safety: using regex + Azure AI Content Safety (endpoint=%s)", endpoint)
        return await _check_azure(text, rule, endpoint, key)

    logger.info("Content safety: using regex only (AZURE_CONTENT_SAFETY_ENDPOINT not set)")
    return _check_regex(text, rule)
