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
