"""LLM-as-Judge rule — evaluates agent output quality against a rubric.

Checks the final draft reply for a configurable set of quality concerns:
  - advice_overreach   : definitive legal/tax conclusions without caveats
  - factuality         : invented policy references, wrong figures
  - completeness       : failed to address every question in the input
  - tone               : overly dismissive, unprofessional, or legally committal

The rubric is injected with the input and output text. The judge LLM returns
a structured verdict (pass/warn/fail per dimension) in a single call.

Configuration via PolicyRule.params in the manifest YAML:
    - name: llm_judge
      stage: post_execution
      action: warn
      checks:
        - advice_overreach
        - factuality
        - completeness
        - tone

The `llm` callable must be injected at registration time via
PolicyEngine.register_rule("llm_judge", make_llm_judge_handler(llm)).
"""

import json
import logging

from ao.policy.engine import PolicyResult
from ao.policy.schema import PolicyRule

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM_PROMPT = """You are a quality reviewer for a government tax authority email assistant.
You will be given the original taxpayer email (INPUT) and the AI-drafted reply (OUTPUT).
Evaluate the output against only the checks listed in CHECKS.

For each check, respond with one of: pass | warn | fail
Also provide a single brief reason (max 15 words) if the result is not "pass".

Respond ONLY with valid JSON in this exact format (no markdown, no explanation outside JSON):
{
  "results": {
    "<check_name>": {"verdict": "pass|warn|fail", "reason": "<reason or empty string>"}
  }
}

CHECK DEFINITIONS:
- advice_overreach : Output makes definitive legal/tax conclusions without recommending professional advice where appropriate.
- factuality       : Output invents policy section numbers, incorrect figures, or regulations not supported by the SOP provided.
- completeness     : Output fails to address one or more distinct questions or issues raised in the input.
- tone             : Output is dismissive, impolite, legally committal, or unprofessional in tone.
"""

_JUDGE_USER_TEMPLATE = """CHECKS: {checks}

INPUT (taxpayer email):
{input}

OUTPUT (AI draft reply):
{output}"""

_DEFAULT_CHECKS = ["advice_overreach", "factuality", "completeness", "tone"]


def make_llm_judge_handler(llm):
    """Factory: returns an async check_llm_judge handler bound to the given LLM provider.

    Usage in app.py:
        from ao.policy.rules.llm_judge import make_llm_judge_handler
        policy_engine.register_rule("llm_judge", make_llm_judge_handler(llm))
    """

    async def check_llm_judge(data: dict, rule: PolicyRule) -> PolicyResult:
        """Evaluate draft output quality using the LLM as a judge."""
        input_text = data.get("input", "")
        output_text = data.get("output", "")

        if not output_text:
            return PolicyResult(rule_name=rule.name, passed=True, action=rule.action,
                                detail="No output to evaluate")

        checks = rule.params.get("checks", _DEFAULT_CHECKS)

        messages = [
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": _JUDGE_USER_TEMPLATE.format(
                checks=", ".join(checks),
                input=input_text[:2000],   # guard against very long emails
                output=output_text[:2000],
            )},
        ]

        try:
            resp = await llm.complete(messages=messages, temperature=0.0)
            raw = resp.content or ""

            # Strip markdown code fences if the LLM wraps in ```json
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            verdict_data = json.loads(raw.strip())
            results = verdict_data.get("results", {})
        except Exception as exc:
            logger.warning("LLM judge call failed: %s", exc)
            return PolicyResult(rule_name=rule.name, passed=True, action=rule.action,
                                detail=f"Judge skipped (LLM error): {exc}")

        failures = []
        warnings = []
        for check, v in results.items():
            verdict = v.get("verdict", "pass")
            reason = v.get("reason", "")
            if verdict == "fail":
                failures.append(f"{check}: {reason}" if reason else check)
            elif verdict == "warn":
                warnings.append(f"{check}: {reason}" if reason else check)

        if failures:
            detail = "LLM judge FAIL — " + "; ".join(failures)
            if warnings:
                detail += " | WARN — " + "; ".join(warnings)
            logger.warning("llm_judge failed for output: %s", detail)
            return PolicyResult(rule_name=rule.name, passed=False, action=rule.action,
                                detail=detail, metadata={"verdicts": results})

        detail = ""
        if warnings:
            detail = "LLM judge WARN — " + "; ".join(warnings)
            logger.info("llm_judge warnings: %s", detail)

        return PolicyResult(rule_name=rule.name, passed=True, action=rule.action,
                            detail=detail, metadata={"verdicts": results})

    return check_llm_judge
