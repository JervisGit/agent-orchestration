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
    config/                     # App manifest loader + schema
      manifest.py               # AppManifest, AgentConfig, ToolConfig dataclasses
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
  email_assistant/              # Backend + Frontend + ao-manifest.yaml
  rag_search/                   # Backend + Frontend + ao-manifest.yaml
  graph_compliance/             # Backend + Frontend + ao-manifest.yaml

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
| 8 | LLM/Agent eval | **DeepEval + Langfuse Evals** | Ragas, custom eval, promptfoo |

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
| Eval | LLM response quality, accuracy | **DeepEval** (pytest plugin) + **Langfuse Evals** |
| Security | Prompt injection, PII leakage, permission boundaries | Red-team test cases, automated injection payloads |
| Load | Concurrent workflow throughput | Locust / k6 |

### Evaluation Framework: DeepEval + Langfuse Evals

**DeepEval** is the primary evaluation harness for development and CI. It provides:

- **14+ LLM metrics**: faithfulness, answer relevancy, hallucination, bias, toxicity, contextual recall/precision
- **LLM-as-judge**: use a reference LLM to score agent outputs (no manual labelling)
- **pytest plugin**: `deepeval test run` integrates into existing test suite and CI pipeline
- **Regression tracking**: compare scores across runs to catch quality degradation
- **Custom metrics**: extend with domain-specific scoring functions

**Langfuse Evals** is used for production trace scoring:

- **Score production traces**: attach eval scores directly to Langfuse traces in real-time
- **Dashboard visibility**: app teams see quality trends per agent/workflow in Langfuse UI
- **Annotation workflows**: human reviewers can score traces for ground-truth labels
- **Triggered evals**: run DeepEval metrics on sampled production traces periodically

| Context | Tool | What it does |
|---|---|---|
| Development (local) | DeepEval | Run eval suite against mock or local LLM (Ollama) |
| CI pipeline | DeepEval | Gate deployments on minimum metric thresholds |
| Production | Langfuse Evals | Score live traces, track quality over time |
| Review cycles | Langfuse Annotations | Human reviewers label traces to build ground-truth data |

Decision tracked in **ADR-004**.

**CI pipeline**: lint → unit → integration → eval (DeepEval) → security scan → build → deploy staging

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

---

## LLM Ownership: Overlay, Not Replace

DSAI apps that already have LLM calls (e.g., RAG search) **keep them as-is**. AO does not replace existing non-agentic LLM usage. Instead:

| Use case | Where LLM lives | Why |
|---|---|---|
| Existing RAG search, summarization | **App** (unchanged) | No disruption, already working |
| New agentic workflows (multi-step, tool-calling, HITL) | **AO** | AO manages agent reasoning, tool selection, orchestration |
| Gradual migration | **App → AO** over time | App teams can optionally migrate existing LLM calls to AO for unified tracing/cost tracking |

AO's `LLMProvider` is for **agent reasoning** (deciding what to do, calling tools, generating responses within a workflow). It is not a mandatory replacement for direct LLM calls in app code.

### Adoption path for an existing app

1. `pip install ao-core` (or add to requirements)
2. Create an `ao-manifest.yaml` in the app repo (see below)
3. Build workflow steps that call AO's engine — existing app code untouched
4. Optionally, migrate existing LLM calls to `AzureOpenAIProvider` for unified tracing

---

## App Onboarding: Config-Driven via Manifests

Each DSAI app registers with AO via a **YAML manifest** (`ao-manifest.yaml`). No code changes to AO core are needed. The manifest declares:

- **App identity** — service principal, identity mode
- **Agents** — name, system prompt, model, tools, temperature
- **Tools** — type, endpoint, connection secret, identity override
- **Policies** — guardrails to apply
- **Observability** — Langfuse project for trace isolation

### Manifest schema

```yaml
app_id: email_assistant                    # Unique app identifier
display_name: Email Assistant
description: Processes inbound emails

# Identity
identity_mode: service                     # "service" or "user_delegated"
service_principal_id: ${SP_CLIENT_ID}      # Entra app registration

# LLM
llm_endpoint: ${AZURE_OPENAI_ENDPOINT}    # Shared or app-specific
llm_api_key_secret: azure-openai-key       # Key Vault secret name

# Observability — each app gets its own Langfuse project
langfuse_project: email-assistant

# Agents — each agent has a system prompt + tool access list
agents:
  - name: email_classifier
    system_prompt: |
      You are an email classification agent...
    model: gpt-4o-mini
    tools: []                              # No tools needed for classification

  - name: reply_drafter
    system_prompt: |
      You are a professional email reply drafter...
    model: gpt-4o
    tools: [knowledge_base]                # Can access the knowledge base tool

# Tools — declarative, no code changes to AO
tools:
  - name: knowledge_base
    type: search_index                     # "api", "database", "search_index", "adls", "custom"
    description: Vector search over company KB
    endpoint: ${AI_SEARCH_ENDPOINT}
    connection_secret: ai-search-api-key   # Key Vault secret name
    params:
      index_name: email-kb
      top_k: 5

  - name: neo4j_query
    type: database
    endpoint: ${NEO4J_URI}
    connection_secret: neo4j-credentials
    identity_mode: user_delegated          # Override: use caller's identity
    params:
      database: compliance
      read_only: true                      # Safety: no writes

  - name: analytics_query
    type: adls
    endpoint: ${SYNAPSE_ENDPOINT}
    connection_secret: synapse-connection
    identity_mode: user_delegated
    params:
      database: analytics
      allowed_tables: [sales_summary, customer_metrics]

  - name: internal_system_api
    type: api
    endpoint: ${INTERNAL_SYSTEM_API_URL}
    connection_secret: internal-api-key
    params:
      timeout_seconds: 30

# Policies
policies:
  - name: pii_filter
    stage: pre_execution
    action: redact
  - name: content_safety
    stage: post_execution
    action: block
  - name: token_budget
    stage: runtime
    max_tokens_per_run: 30000
```

### Adding a new agent

Edit `ao-manifest.yaml` — add an entry under `agents:` with a name, system prompt, model, and tool access list. No AO code changes.

### Adding a new tool

Edit `ao-manifest.yaml` — add an entry under `tools:` with type + connection details. For **standard types** (`api`, `database`, `search_index`, `adls`), AO provides built-in adapters. For **custom types**, implement a tool function in the app and register it via `ToolRegistry`.

### Identity assignment

Each app has an **Entra service principal** (`service_principal_id`). This is the app's identity in Azure. The `identity_mode` at manifest level sets the default; individual tools can override it (e.g., Neo4j tool uses `user_delegated` even if the app default is `service`).

---

## Tool Architecture

Tools are the interface between agents and external systems. AO provides built-in adapters for common tool types:

| Tool type | Adapter | External system |
|---|---|---|
| `api` | REST client with auth | Any REST API (internal systems) |
| `database` | DB query executor | Neo4j, PostgreSQL, SQL Server |
| `search_index` | Vector/hybrid search | Azure AI Search, pgvector |
| `adls` | Spark SQL via Synapse | Delta tables in ADLS |
| `custom` | User-provided function | Anything else |

### MCP (Model Context Protocol) — Future consideration

Currently, tools are registered via `ao-manifest.yaml` → `ToolRegistry`. In the future, AO could expose an **MCP server** so that agents can discover and invoke tools through the MCP standard. This would allow:

- External agent frameworks to consume AO's tools
- AO agents to consume external MCP servers (e.g., from partner teams)

For now, the `ToolRegistry` approach is simpler and sufficient. MCP can be layered on top without breaking changes. Tracked as a future ADR.

---

## Observability RBAC: Per-App Trace Isolation

Each DSAI app maps to a **Langfuse project**. Langfuse supports project-level access control:

| Role | Access | Example |
|---|---|---|
| App developer | Read traces for **their app's project only** | Email assistant team sees only `email-assistant` project |
| AO platform admin | Read all projects | Platform team sees all traces for debugging cross-app issues |
| Auditor | Read-only across projects | Compliance review of all LLM interactions |

### How it works

1. App's `ao-manifest.yaml` declares `langfuse_project: email-assistant`
2. AO's `AOTracer` routes traces to that project automatically
3. Langfuse RBAC (backed by Entra ID SSO) controls who can access which project
4. AO Dashboard proxies Langfuse data with additional RBAC filtering

### What each app team can see

- All traces/spans for their app's workflows
- LLM call details: prompts, completions, token usage, latency, cost
- Evaluation scores and error rates
- **Cannot** see other apps' traces
