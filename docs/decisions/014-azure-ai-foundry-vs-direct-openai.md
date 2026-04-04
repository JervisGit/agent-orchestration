# ADR-014: Azure AI Foundry Agent Service vs Direct Azure OpenAI + DIY Framework

## Status
Accepted

## Context

Azure AI Foundry (formerly Azure AI Studio) now offers a managed **Agent Service** (Assistants API-compatible) that handles thread management, tool calling, file retrieval, and code interpreter out of the box. As the AO platform matures, we need a standing decision on when to use the managed service versus our own ManifestExecutor-on-LangGraph stack.

---

## Options Considered

### Option A — Azure AI Foundry Agent Service (Assistants API)
Managed agents hosted by Microsoft. Agents have built-in thread/message persistence, a file store, code interpreter, and a tool-calling loop. Identity and authorisation are handled by Azure AI Foundry's own project-scoped managed identity.

### Option B — Azure AI Foundry Prompt Flow
YAML-defined DAG or LLM flows deployed as managed online endpoints. Good for simple prompt → response pipelines; limited support for dynamic multi-agent loops.

### Option C — Azure OpenAI + AO ManifestExecutor (current decision)
We call the Azure OpenAI Chat Completions API directly and build orchestration in LangGraph via `ManifestExecutor`. Identity is via UAMI per agent + APIM + Entra App Roles (ADR-013).

---

## Decision

**Continue with Option C** for the current workloads.

---

## Rationale

### Why not Azure AI Foundry Agent Service right now

| Concern | Detail |
|---|---|
| **Lock-in to Microsoft threading model** | Foundry threads persist in Azure storage you don't control; migrating or auditing raw state is difficult. AO's state schema (LangGraph dict) is portable. |
| **Identity model mismatch** | Foundry agents use the AI project's shared managed identity. AO requires per-agent UAMI isolation (ADR-013) for blast-radius containment — not possible with the shared project identity today. |
| **Policy enforcement gap** | Foundry has no hook to inject custom pre/post-execution policy guards (content safety, PII filter, LLM judge). Our `PolicyEngine` runs inside the graph; it cannot intercept Foundry's internal tool loop. |
| **Observability coupling** | Foundry exports traces to Azure Monitor / Application Insights. We use Langfuse for span-level, per-agent tracing with custom metadata. Dual-tracing would be complex and expensive. |
| **Agent lifecycle ownership** | `ao-manifest.yaml` lets app teams onboard without code changes; Foundry agents require portal or SDK configuration per agent, with no equivalent YAML-first lifecycle. |
| **Orchestration patterns** | AO supports router, concurrent, supervisor, linear patterns with the same executor. Foundry Agent Service is single-agent unless you wire up Semantic Kernel multi-agent on top manually. |
| **Vendor API surface** | Foundry Agents API surface is still evolving (preview). Building on it now risks rework as APIs stabilise. |

### Why Option C works well

- **Full state ownership** — LangGraph graph state is a plain Python dict; checkpointed to Redis or MemorySaver; inspectable, portable.
- **Per-agent identity** — UAMI per agent type enforced at APIM; zero blanket permissions.
- **Config-driven onboarding** — new apps provide a YAML manifest; no code changes to AO core.
- **Composable policies** — `PolicyEngine` wraps any workflow; runs pre/post execution with block, warn, redact, judge actions.
- **LLM provider flexibility** — swap Azure OpenAI for OpenAI or a self-hosted model by changing the LLM class; orchestration stays unchanged.
- **Reasoning visibility** — `show_reasoning: true` in manifest exposes CoT thinking in the UI; not natively surfaced in Foundry.

---

## When to reconsider Azure AI Foundry Agent Service

Revisit if **any** of the following hold:

1. **Code Interpreter is required** — Foundry's sandboxed Python execution is hard to replicate safely; use Foundry for workloads needing dynamic code execution.
2. **Built-in file search at scale** — Foundry vector stores handle large file corpora with automatic chunking; worth it if rag_search grows beyond our pgvector setup.
3. **Per-agent UAMI support is added** — if Foundry allows separate managed identities per agent, the identity isolation gap closes.
4. **Policy hooks are exposed** — if Foundry exposes pre/post-call middleware, we can port `PolicyEngine` guards.
5. **Rapid prototyping with no infra** — Foundry removes infra overhead for greenfield PoCs; use it to prototype then evaluate migration to AO.

---

## Azure AI Agent Service Lifecycle vs AO Lifecycle

| Lifecycle Stage | Azure AI Foundry Agent Service | AO (current) |
|---|---|---|
| **Onboarding** | Portal / SDK per agent | `ao-manifest.yaml` (YAML-first) |
| **Identity** | Shared project managed identity | UAMI per agent type (ADR-013) |
| **Orchestration** | Single agent + Assistants thread loop | LangGraph graph; router/concurrent/supervisor/linear |
| **Tools** | OpenAPI spec or built-in (file search, code interpreter) | Registered callables; schema-validated; APIM-gated |
| **State** | Azure-managed thread messages | Plain dict; Redis or MemorySaver checkpointer |
| **Policies** | None (must add Prompt Shields separately) | `PolicyEngine` pre/post execution; pluggable rules |
| **Observability** | Azure Monitor / App Insights | Langfuse (span-level, per-agent, custom metadata) |
| **Eval** | Azure AI Evaluation SDK | DeepEval + Langfuse Evals (ADR-004) |
| **HITL** | Not native (build separately) | First-class: `hitl_condition`, `ao_hitl_requests` table, dashboard |
| **Multi-agent** | Semantic Kernel orchestrator layer needed | Native: supervisor / concurrent patterns in ManifestExecutor |
| **Deployment** | Foundry-managed compute | ACA or AKS (ADR-005, ADR-011) |

---

## Consequences

- AO teams maintain the LangGraph execution layer and the ManifestExecutor; no free runtime upgrades from Microsoft.
- When a new feature ships in Azure AI Foundry (e.g. built-in memory, file search), AO must implement an equivalent or explicitly decide to use Foundry for that capability.
- Cost: Azure OpenAI token pricing is the same regardless of whether Foundry is in the path; the main overhead is the APIM gateway (negligible at current scale).
