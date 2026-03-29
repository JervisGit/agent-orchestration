# Agent Orchestration (AO) Layer — Architecture Plan

## TL;DR

Build a shared AO SDK (`ao-core` Python package) + Platform Services that DSAI apps import, avoiding per-app re-implementation. **LangGraph** as orchestration engine, **Langfuse** (self-hosted on AKS) for LLM observability, **FastAPI** for platform API, **Vue.js** dashboard for trace debugging + HITL approvals.

---

## Repo Structure

**AO repo** (this repo):

```
ao-core/                        # Python SDK — published as internal package
  ao/
    engine/                     # Orchestration engine abstraction + LangGraph impl
      base.py                   # Abstract OrchestrationEngine interface
      langgraph_engine.py       # LangGraph implementation
      patterns/                 # Pre-built: linear, router, supervisor, planner
    identity/                   # IdentityContext (UserDelegated | ServiceIdentity)
    policy/                     # Guardrails — declarative YAML policies, eval engine
    memory/                     # Short-term (Redis), long-term (PG+pgvector), knowledge, shared
    hitl/                       # Approval flows, notification channels
    tools/                      # Tool registry + identity-scoped execution
    resilience/                 # Checkpointing, retry, circuit breaker, dead-letter
    observability/              # OpenTelemetry + Langfuse integration, @trace decorators
    llm/                        # LLM provider abstraction (Azure OpenAI, Foundry, AWS)
  pyproject.toml

ao-platform/                    # Hosted services
  api/                          # FastAPI — workflow mgmt, HITL endpoints, admin
  dashboard/                    # Vue.js — trace viewer, debugging UI, HITL queue, policy editor
  workers/                      # Background: dead-letter processing, eval jobs

infra/                          # Terraform
  modules/                      # aks/, database/, messaging/, observability/, security/, ai/
  environments/                 # dev.tfvars, staging.tfvars, prod.tfvars
  main.tf

tests/
  unit/  integration/  eval/  security/

examples/                       # DSAI app clones for demo/testing
  email_assistant/              # Backend + Frontend
  rag_search/                   # Backend + Frontend
  graph_compliance/             # Backend + Frontend

docs/
  architecture.md               # This document
  decisions/                    # ADRs tracking every decision + alternatives

docker/
  Dockerfile.ao-api
  Dockerfile.ao-worker
  docker-compose.local.yml      # Local dev environment
```

**Production**: each DSAI app lives in its own repo and imports `ao-core` as a package.

---

## Key Architecture Decisions

| # | Decision | Chosen | Alternatives (tracked in ADRs) |
|---|---|---|---|
| 1 | Orchestration framework | **LangGraph** | Semantic Kernel, AutoGen, CrewAI |
| 2 | LLM observability | **Langfuse** (self-hosted on AKS) | LangSmith, Phoenix/Arize, Azure AI Foundry Tracing |
| 3 | State store | **PostgreSQL + Redis** | Cosmos DB, Redis-only |
| 4 | Architecture model | **SDK + platform services** | Pure SDK, pure platform/SaaS |
| 5 | Async messaging | **Azure Service Bus** | Event Hubs, Storage Queues |
| 6 | Auth | **Entra ID** (OBO + Managed Identity) | — |
| 7 | Hosting | **AKS** (existing cluster) | Container Apps, App Service |

---

## Core Modules

### Orchestration Engine

Abstract interface with LangGraph implementation. Apps select patterns (`LinearChain`, `RouterAgent`, `SupervisorMultiAgent`, `PlanAndExecute`) via config or code. Framework-swappable via the abstraction.

### Identity

`IdentityContext` with two modes:

- **`UserDelegated`** — Entra OBO flow, actions run as the user (e.g., graph compliance — internal officers)
- **`ServiceIdentity`** — Managed Identity, least-privilege RBAC (e.g., email assistant — processing external mail)

Identity is bound **per tool invocation**; policy validates permissions before execution.

### Policy / Guardrails

Declarative YAML per app, evaluated at three stages:

- `pre_execution`: input validation, PII detection/redaction, allowed-actions whitelist
- `post_execution`: output filtering, content safety (Azure AI Content Safety)
- `runtime`: token budget enforcement, rate limiting, circuit breakers

```yaml
policies:
  - name: pii_filter
    stage: pre_execution
    action: redact
  - name: content_safety
    stage: post_execution
    provider: azure_content_safety
  - name: token_budget
    stage: runtime
    max_tokens_per_run: 50000
```

### Memory

- **Short-term**: Redis — conversation context, session state (TTL eviction)
- **Long-term**: PostgreSQL + pgvector — persistent facts, preferences, embeddings
- **Knowledge**: Abstract RAG interface (each app provides its own knowledge base)
- **Inter-agent**: Shared state in workflow context + async messages via Service Bus

### HITL (Human-in-the-Loop)

- Per-step config: `required | optional | auto`
- Notification channels: WebSocket (dashboard), webhook, email
- Timeout + fallback: no human response in X minutes → escalate / fallback action
- Toggle on/off per environment (auto-approve in dev, require in prod)

### Resilience

- LangGraph checkpointing → resume from last successful step
- Idempotent tool execution with deduplication keys
- Retry policies (exponential backoff + jitter)
- Dead-letter queue (Service Bus) for failed steps
- Graceful degradation: non-critical failures → continue with fallback

### LLM Abstraction

- Thin wrapper around LLM providers (Azure OpenAI, Foundry, AWS Bedrock)
- Retries, token counting, cost tracking per call
- Model routing: policy specifies which model per task (GPT-4o for reasoning, GPT-4o-mini for summarization)

---

## Observability Strategy

Traditional monitoring (Azure Monitor) is insufficient for LLM apps — you need to trace prompts, completions, token usage, latency per LLM call, and costs.

| Layer | Tool | Purpose |
|---|---|---|
| LLM-specific | **Langfuse** (self-hosted on AKS) | Prompt/completion pairs, tokens, cost, eval scores, trace trees |
| Distributed tracing | **OpenTelemetry SDK** | Correlate AO calls with app calls across services |
| Infrastructure | **Azure Monitor / App Insights** | CPU, memory, request rates, errors |
| Debugging | **AO Dashboard** (Vue.js) | Workflow-level step-by-step trace viewer |

---

## Infrastructure (Terraform)

| Resource | Purpose |
|---|---|
| AKS namespace | AO API, workers, Langfuse |
| PostgreSQL Flexible Server | Long-term memory, workflow state, audit |
| Azure Cache for Redis | Short-term memory, caching |
| Azure Service Bus | Inter-agent comms, dead-letter, resilience |
| Azure Key Vault | Secrets, API keys |
| Azure OpenAI / Foundry | LLM endpoints |
| Entra ID | App registrations, managed identities |
| Azure Container Registry | AO + app images |
| Azure Monitor + App Insights | Infra monitoring |

---

## Testing & LLMSecOps

| Category | What | How |
|---|---|---|
| Unit | Policy engine, identity flows, memory ops | pytest, mock LLM calls |
| Integration | E2E workflow execution | Test LLM endpoint, real Redis/PostgreSQL |
| Eval | LLM response quality, accuracy | Reference answers, LLM-as-judge |
| Security | Prompt injection, PII leakage, permission boundaries | Red-team test cases, automated injection payloads |
| Load | Concurrent workflow throughput | Locust / k6 |

**CI pipeline**: lint → unit → integration → eval → security scan → build → deploy staging

---

## Implementation Phases

### Phase 1 — Foundation (MVP)

1. Scaffold `ao-core` package with `OrchestrationEngine` interface + `LangGraphEngine`
2. `LinearChain` pattern
3. `ServiceIdentity` mode (Managed Identity)
4. Basic policy engine (token budget + content safety)
5. Short-term memory (Redis)
6. OTel + Langfuse tracing
7. Local docker-compose (Redis, PG, Langfuse)

*Verify: email assistant demo runs a linear chain with traces visible in Langfuse*

### Phase 2 — Resilience & HITL

8. Checkpointing + retry policies
9. HITL manager + WebSocket channel
10. Dead-letter queue (Service Bus)
11. `UserDelegated` identity mode (OBO flow)

*Verify: workflow fails mid-step and resumes; HITL approval blocks then resumes*

### Phase 3 — Platform & Advanced Patterns

12. AO Platform API (FastAPI)
13. Dashboard (Vue.js) — trace viewer + HITL queue *(parallel with 12)*
14. Multi-agent supervisor + plan-and-execute patterns
15. Long-term memory (PG + pgvector)
16. Inter-agent communication

*Verify: graph compliance app uses multi-agent pattern with user-delegated identity*

### Phase 4 — Hardening

17. Full policy YAML schema + dashboard policy editor
18. Eval suite + security red-teaming
19. Terraform for all infra
20. CI/CD pipeline
21. Load testing

*Verify: CI green, security tests pass, load test meets targets*

---

## Scope

- **In**: AO SDK, platform API, dashboard, infra, DSAI app demos
- **Out**: Individual DSAI app business logic (own repos in prod), ML training, data pipelines
