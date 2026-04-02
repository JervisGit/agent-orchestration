# ADR-012: Input Safety Guardrails — Tooling & Provider Decision

## Status
Accepted (Phase 1 implemented; Phase 2 tooling selected and provisioned in Terraform)

## Context
The AO Layer processes user-supplied text (emails, chat messages) that may contain:
- **Prompt injection / jailbreak attempts** — instructions trying to override the agent's
  system prompt or role (e.g. "ignore previous instructions", DAN-style overrides)
- **Toxic / abusive content** — threats, harassment, explicit language
- **Bias bait** — inputs designed to elicit discriminatory or politically charged responses
- **PII in inputs and outputs** — TINs, emails, phone numbers that may need protection at
  different pipeline stages
- **Advice overreach in outputs** — agent making definitive legal/tax statements instead of
  guiding the user to professional advice (Conflict-of-Interest / CoI category)

The policy engine (`PolicyEngine`, `PolicySet`, `PolicyStage`) already exists with a
pre/post/runtime stage model. The question is which provider to use for detection.

---

## Options Considered

### 1. Regex / Keyword Rules (current — dev placeholder)
**Pros:** Zero latency, zero cost, no external dependency  
**Cons:** Easily bypassed, no semantic understanding, requires constant maintenance  
**Verdict:** Sufficient for local dev only. Production must add a semantic layer.

---

### 2. Azure AI Content Safety + Prompt Shield ✅ Selected for production
**What it provides:**
- **Content Safety** — classifies text into Hate, Violence, Sexual, Self-Harm categories
  with configurable severity thresholds (0–7)
- **Prompt Shield** — dedicated jailbreak / prompt injection detector; returns
  `"attackDetected": true/false` on both user messages and grounded documents
- **Groundedness Detection** — checks if a model's output is factually grounded in the
  provided context (useful for CoI/advice-overreach detection)

**Why this fits:**
- Azure-native → data stays within the tenant (consistent with Langfuse ADR-002 decision)
- Simple REST API (or `azure-ai-contentsafety` SDK), `async`-compatible
- Per-call cost (~$0.75–$1.50 / 1000 calls) is acceptable at current volume
- Prompt Shield is specifically designed for agentic systems (Microsoft's own recommendation)

**Integration point:** Replace/augment the `check_content_safety` handler in
`ao/policy/rules/content_safety.py`. The existing `PolicyEngine` and `PolicySet` YAML
require no changes — only the rule handler changes.

```python
# Planned: ao/policy/rules/content_safety.py (Phase 2)
import os
from azure.ai.contentsafety.aio import ContentSafetyClient
from azure.core.credentials import AzureKeyCredential

async def check_content_safety(data: dict, rule: PolicyRule) -> PolicyResult:
    endpoint = os.environ["AZURE_CONTENT_SAFETY_ENDPOINT"]
    key      = os.environ["AZURE_CONTENT_SAFETY_KEY"]
    text     = data.get("input", "") or data.get("output", "")

    async with ContentSafetyClient(endpoint, AzureKeyCredential(key)) as client:
        # Prompt Shield for jailbreak detection
        shield = await client.shield_prompt(user_prompt=text, documents=[])
        if shield.user_prompt_attack_detected:
            return PolicyResult(rule_name=rule.name, passed=False,
                                action=rule.action, detail="Prompt injection detected")

        # Content categories
        result = await client.analyze_text({"text": text, "categories": ["Hate","Violence","Sexual","SelfHarm"]})
        for cat in result.categories_analysis:
            if cat.severity >= rule.params.get("severity_threshold", 4):
                return PolicyResult(rule_name=rule.name, passed=False,
                                    action=rule.action, detail=f"{cat.category} (severity {cat.severity})")

    return PolicyResult(rule_name=rule.name, passed=True, action=rule.action)
```

---

### 3. OpenAI Moderation API
**Pros:** Free, high accuracy for toxicity/hate  
**Cons:** Data leaves the Azure tenant — **rejected** for same reason Langfuse Cloud was
replaced. Incompatible with internal network data residency requirements (see ADR-002).

---

### 4. Microsoft Presidio (PII)
**What it provides:** Named-entity recognition for PII — email, phone, credit card, SSN,
names, addresses, national IDs — with confidence scoring and redaction

**Why deferred:** The current regex-based `check_pii` handles the known patterns (email,
phone, Singapore NRIC). The critical PII in this domain is the TIN — which must
**not** be redacted from inputs (the agent needs it for DB lookup), only from outputs.

**Phase 2:** Replace `ao/policy/rules/pii.py` with Presidio for completeness. The
stage-differentiated policy approach (WARN on pre_execution, REDACT on post_execution)
already handles the TIN use-case correctly at the architecture level.

```python
# azure-ai-language also has built-in PII detection as an alternative
# pip install azure-ai-textanalytics
```

---

### 5. LLM-as-Judge (Output Quality Review)
**What it provides:** A second LLM call that evaluates the agent's draft output against a
domain-specific rubric. Covers a broader set of output quality concerns beyond CoI:

| Check | Example failure |
|---|---|
| **CoI / Advice overreach** | "You are not liable for this tax" instead of "you may wish to seek advice" |
| **Factual accuracy** | Citing a tax relief that doesn't apply to the taxpayer's filing status |
| **Hallucination** | Inventing a policy section number or deadline |
| **Completeness** | Answering only one of two questions raised in the email |
| **Tone** | Overly dismissive or legally committal language |

The rubric is a short (~200-token) prompt injected with the domain context. A single
`pass/fail/warn` response plus a brief reason is returned — no lengthy generation needed.

**Integration:** New rule handler `check_llm_judge` registered in `PolicyEngine`;
evaluated at `post_execution` stage. Calls the same LLM already configured for the
application (no new infrastructure).

**Why deferred:** Adds ~1–2s latency and one LLM call per response. Acceptable in
production; not justified in the current dev phase.

---

### 6. NeMo Guardrails / LlamaGuard (self-hosted)
**Pros:** Fully self-hosted, no per-call cost  
**Cons:** Infrastructure overhead (another container, GPU optional but preferred),
complex configuration, engineering cost. Not justified vs Azure AI Content Safety
given we are already Azure-first (see ADR-005).

---

## Decision Summary

| Stage | Concern | Phase 1 (current) | Phase 2 |
|---|---|---|---|
| `pre_execution` | Jailbreak / prompt injection | Regex patterns (expanded) | Azure AI Content Safety Prompt Shield |
| `pre_execution` | Toxicity / hate / threats | Regex patterns (expanded) | Azure AI Content Safety |
| `pre_execution` | PII in input | WARN only (keep TIN for lookup) | Presidio (WARN) |
| `pre_execution` | Bias bait | Regex patterns | Azure AI Content Safety Hate category |
| `post_execution` | PII in output | Regex REDACT | Presidio REDACT |
| `post_execution` | CoI / advice overreach | Not implemented | LLM-as-Judge |
| `post_execution` | Factuality / hallucination / completeness | Not implemented | LLM-as-Judge |
| Tool call layer | Tool argument schema validation | Not validated at call time | `jsonschema` validation in `_execute_tool_call` — bad args returned to LLM for self-correction |

## PII Design Note
TINs (e.g. `SG-T001-2890`) must survive the `pre_execution` stage intact so that the
`lookup_taxpayer` tool can query the database. The `pre_execution` PII rule uses
`action: warn` (detect + log, do not redact). The `post_execution` PII rule uses
`action: redact` so TINs are masked in the draft reply sent back to the human.

## CoI / Advice Overreach Note
In the tax domain, "conflict of interest" means the agent making definitive statements
that should be caveated (e.g. "you are exempt from this tax" without adding "please
confirm with a qualified advisor"). This is a semantic problem, not a keyword problem.
LLM-as-Judge with a short rubric is the right tool. Deferred to Phase 2.

## Consequences
- `AZURE_CONTENT_SAFETY_ENDPOINT` and `AZURE_CONTENT_SAFETY_KEY` added to Terraform
  secrets in Phase 2 (new `ai` module resource: `azurerm_cognitive_account`)
- Phase 1 regex expansion is backward-compatible; no new env vars required
- The `PolicyEngine.evaluate()` call in `app.py` is fixed (was using wrong keyword args)
- All blocked inputs are traced as Langfuse spans with `block_reason` metadata
