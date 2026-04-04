# ADR-004: DeepEval + Langfuse Evals for LLM/Agent Evaluation

## Status
Accepted

## Context
LLM applications require evaluation beyond traditional unit/integration testing. We need to measure output quality (faithfulness, relevancy, hallucination), detect regressions, and track quality in production. A hand-rolled keyword-matching eval is insufficient for real LLM outputs.

Requirements:
- Metric-driven evaluation (not just pass/fail)
- LLM-as-judge capability (no manual labelling at scale)
- CI integration (gate deployments on quality thresholds)
- Production trace scoring (monitor live quality)
- RAG-specific metrics (for rag_search app)

## Decision
Use **DeepEval** as the primary evaluation framework for development and CI, combined with **Langfuse Evals** for production trace scoring.

### DeepEval (development + CI)
- pytest plugin: `deepeval test run tests/eval/`
- 14+ built-in metrics: faithfulness, answer relevancy, hallucination, bias, toxicity, contextual recall/precision
- Custom metrics for domain-specific scoring
- Regression tracking across runs
- Supports local LLMs (Ollama) as judge models

### Langfuse Evals (production)
- Attach scores to production traces
- Dashboard visibility per app/agent/workflow
- Human annotation workflows for ground-truth
- Periodic eval runs on sampled traces

## Alternatives Considered

| Tool | Pros | Cons |
|---|---|---|
| **DeepEval** | Rich metrics, pytest plugin, LLM-as-judge, CI-friendly | Adds a dependency |
| Ragas | Excellent RAG-specific metrics | Narrower scope (RAG only), less CI integration |
| promptfoo | Config-driven, good for prompt testing | Less suited for agent workflow evaluation |
| Custom eval (current) | No dependencies | Keyword matching only, no LLM-as-judge, doesn't scale |
| Langfuse Evals alone | Already deployed | No CI integration, no regression tracking |

## Consequences
- `deepeval` added as a dev dependency in `pyproject.toml`
- Eval test suite uses DeepEval metrics instead of keyword matching
- CI pipeline gates on minimum metric thresholds (e.g., faithfulness >= 0.7)
- Production traces scored via Langfuse SDK integration in `AOTracer`
- App teams can define custom metrics per app in their eval suites

---

## Addendum: Manifest-First Eval and Red-Teaming (App-Level Ownership)

### Problem
Current evals and red-team tests live in the AO platform repo (`tests/eval/`, `tests/security/`).
This works for the shared framework but breaks down as DSAI apps proliferate:

- App developers have the domain knowledge to write meaningful eval cases (e.g. "an assessment-relief reply must cite the 30-day objection window").
- Red-team prompts are agent-specific — a jailbreak relevant to a tax assistant is different from one for a compliance assistant.
- Centralising all evals creates a bottleneck; distributed ownership scales better.

### Two Models

**Option A — App adds agents in its own repo (manifest submission)**
- Each DSAI app repo maintains its own `ao-manifest.yaml` + eval suite.
- App repo CI runs: `deepeval test run tests/eval/` and `pytest tests/security/`.
- AO platform provides the evaluation SDK (deepeval wrapper, policy engine) as a pip-installable library.
- Red-team config lives in `promptfoo/redteam.yaml` in the app repo, pointing at the app's ACA endpoint.
- AO CI only tests the framework itself (unit + integration).

**Option B — App adds agents by submitting to AO platform (registry model)**
- See ADR-007 for agent registry / AgentOps model.
- Eval cases and red-team prompts are declared in the agent registration payload (YAML/JSON).
- AO platform runs evals centrally on behalf of the app, using its own LLM judge credentials.
- Each registered agent has a quality gate (metric thresholds) that must pass before promotion to production.

### Decision
Both models are supported. The default is **Option A** (each app repo owns its evals).
Option B becomes viable once the agent registry (ADR-007 Option B) is built.

### Guidance for App Teams (Option A)
```yaml
# in the app repo: ao-manifest.yaml
eval:
  metrics:
    faithfulness: 0.7
    answer_relevancy: 0.7
    hallucination: 0.3
  test_cases: tests/eval/

redteam:
  promptfoo_config: promptfoo/redteam.yaml
  owasp_coverage: [LLM01, LLM02, LLM06, LLM08]
```
The AO SDK reads `eval:` and `redteam:` blocks to auto-configure thresholds and PromptFoo targets.

