# ADR-002: Langfuse for LLM Observability

## Status
Accepted

## Context
Traditional monitoring (Azure Monitor) is insufficient for LLM applications. We need to trace prompts, completions, token usage, latency per LLM call, costs, and evaluation scores. The tool must be self-hostable (data stays in our Azure tenant).

## Decision
Use **Langfuse** (self-hosted as an Azure Container Apps container in the same ACA environment) for LLM-specific observability, combined with OpenTelemetry for distributed tracing and Azure Monitor for infrastructure metrics.

## Alternatives Considered

| Tool | Pros | Cons |
|---|---|---|
| **Langfuse** | OSS, self-hostable, rich LLM traces, eval scores, cost tracking | Extra infra to maintain |
| LangSmith | Deep LangChain integration | Vendor-hosted (data leaves tenant), paid |
| Phoenix / Arize | Good eval framework | Heavier, less focus on tracing |
| Azure AI Foundry Tracing | Azure-native | Newer, less mature, limited trace visualization |
| Pydantic Logfire | Production-grade, not just LLM but whole stack observability | Requires contract for enterprise (potential vendor lock-in) |

## Consequences
- Langfuse is a persistent container in the ACA environment (`min_replicas = 1`); adds ~\$15–25/month at B1 sizing
- Data stays within Azure tenant (security requirement met) — no trace data touches cloud.langfuse.com
- Project API keys are Terraform-managed (`random_uuid`); retrieve via `terraform output langfuse_public_key`
- OTel SDK adds instrumentation overhead (minimal)
