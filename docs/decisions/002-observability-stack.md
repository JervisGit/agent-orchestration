# ADR-002: Langfuse for LLM Observability

## Status
Accepted

## Context
Traditional monitoring (Azure Monitor) is insufficient for LLM applications. We need to trace prompts, completions, token usage, latency per LLM call, costs, and evaluation scores. The tool must be self-hostable (data stays in our Azure tenant).

## Decision
Use **Langfuse** (self-hosted on AKS) for LLM-specific observability, combined with OpenTelemetry for distributed tracing and Azure Monitor for infrastructure metrics.

## Alternatives Considered

| Tool | Pros | Cons |
|---|---|---|
| **Langfuse** | OSS, self-hostable, rich LLM traces, eval scores, cost tracking | Extra infra to maintain |
| LangSmith | Deep LangChain integration | Vendor-hosted (data leaves tenant), paid |
| Phoenix / Arize | Good eval framework | Heavier, less focus on tracing |
| Azure AI Foundry Tracing | Azure-native | Newer, less mature, limited trace visualization |
| Pydantic Logfire | Production-grade, not just LLM but whole stack observability | Requires contract for enterprise (potential vendor lock-in) |

## Consequences
- Need to deploy + maintain Langfuse on AKS
- Data stays within Azure tenant (security requirement met)
- OTel SDK adds instrumentation overhead (minimal)
