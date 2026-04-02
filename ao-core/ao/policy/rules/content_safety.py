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
                )
    return PolicyResult(rule_name=rule.name, passed=True, action=rule.action)


# ── Azure AI Content Safety (Phase 2) ────────────────────────────

async def _check_azure(text: str, rule: PolicyRule, endpoint: str, key: str) -> PolicyResult:
    """Call Azure AI Content Safety: Prompt Shield + category analysis."""
    from azure.ai.contentsafety.aio import ContentSafetyClient
    from azure.ai.contentsafety.models import (
        AnalyzeTextOptions,
        ShieldPromptOptions,
        TextCategory,
    )
    from azure.core.credentials import AzureKeyCredential
    from azure.core.exceptions import HttpResponseError

    severity_threshold: int = rule.params.get("severity_threshold", 4)

    try:
        async with ContentSafetyClient(endpoint, AzureKeyCredential(key)) as client:
            # 1. Prompt Shield — jailbreak / indirect attack detection
            try:
                shield_result = await client.shield_prompt(
                    ShieldPromptOptions(user_prompt=text, documents=[])
                )
                if shield_result.user_prompt_analysis and shield_result.user_prompt_analysis.attack_detected:
                    return PolicyResult(
                        rule_name=rule.name,
                        passed=False,
                        action=rule.action,
                        detail="Content safety violation [jailbreak] (Prompt Shield): attack detected",
                    )
            except Exception as exc:
                logger.warning("Prompt Shield call failed, continuing to category check: %s", exc)

            # 2. Content category analysis
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
                    return PolicyResult(
                        rule_name=rule.name,
                        passed=False,
                        action=rule.action,
                        detail=(
                            f"Content safety violation [{item.category}] "
                            f"(Azure AI): severity {item.severity} >= threshold {severity_threshold}"
                        ),
                    )

    except HttpResponseError as exc:
        logger.error("Azure Content Safety API error: %s — falling back to regex", exc)
        return _check_regex(text, rule)
    except Exception as exc:
        logger.error("Unexpected error calling Azure Content Safety: %s — falling back to regex", exc)
        return _check_regex(text, rule)

    return PolicyResult(rule_name=rule.name, passed=True, action=rule.action)


# ── Public handler ────────────────────────────────────────────────

async def check_content_safety(data: dict, rule: PolicyRule) -> PolicyResult:
    """Check input/output text for unsafe content.

    Uses Azure AI Content Safety when env vars are configured,
    otherwise falls back to regex patterns (local dev / free-trial).
    """
    text = data.get("input", "") or data.get("output", "")
    if not isinstance(text, str):
        text = str(text)

    endpoint = os.environ.get("AZURE_CONTENT_SAFETY_ENDPOINT", "")
    key = os.environ.get("AZURE_CONTENT_SAFETY_KEY", "")

    if endpoint and key:
        logger.debug("Content safety: using Azure AI Content Safety")
        return await _check_azure(text, rule, endpoint, key)

    logger.debug("Content safety: AZURE_CONTENT_SAFETY_ENDPOINT not set — using regex fallback")
    return _check_regex(text, rule)


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
