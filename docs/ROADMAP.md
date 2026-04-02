# Agent Orchestration Platform — Development Roadmap

## Phase 1 — Foundation ✅
*Goal: AO SDK + platform skeleton running locally.*

- AO SDK core: LLM providers (OpenAI, Azure OpenAI, Ollama), memory, policy engine,
  identity (Entra), HITL manager, resilience (retry/fallback/checkpoint), tools
- AO Platform API (FastAPI, port 8000) + Dashboard (overview, workflows, policies, HITL, traces)
- Email Assistant demo (port 8001) — 5 specialist agents, PostgreSQL taxpayer lookup,
  SSE streaming, Langfuse tracing
- Terraform infra modules (ACA, AKS, AI, database, messaging, observability, security)
- 78 unit / integration / eval / load tests
- ADRs 001–006

## Phase 2 — Manifest-Driven Engine ✅
*Goal: app teams declare agents + SOPs in YAML; no LangGraph code in app repos.*

- ADR-007: config-driven agents decision
- `AgentConfig`: `sop`, `hitl_condition`, `hitl_action`, `trace_metadata` fields
- `AppManifest`: `pattern`, `classifier_agent`, `intent_agents` fields
- `ManifestExecutor`: reads `ao-manifest.yaml`, builds LangGraph automatically
  - `router` pattern: classify → one specialist → END
  - `concurrent` pattern: detect all intents → parallel dispatch
    → LLM merge → END; merge node has Langfuse generation span
- Pattern library: `router`, `linear`, `supervisor`, `planner`, `concurrent`
- Email assistant: `app.py` uses `ManifestExecutor`; `ao-manifest.yaml` declares
  5 specialists with SOPs; LangGraph isolated entirely inside `ao-core`
- HITL end-to-end: `hitl_condition` in manifest → `_persist_hitl_request()` → DB row
  → Dashboard HITL queue → approve/reject → `action_webhook` → taxpayer notes updated
  → HITL resolution event written back to originating Langfuse trace ✅
- Dashboard: HITL table with taxpayer name + proposed action + expandable detail
- Email assistant UI: collapsible agent workflow panel, HITL approve/reject buttons,
  conditional DB lookup (skips if no TIN in email body), formal MS-style styling ✅
- Sample emails: em-006 (HITL demo), em-007 (multi-intent concurrent pattern demo)

## Phase 3 — Production Readiness ✅ (infrastructure)
*Goal: safe to deploy, observable in Azure.*

- [x] HITL escalation events traced (resolution event on originating Langfuse trace)
- [x] Container images updated to Python 3.13, correct runtime deps
- [x] `Dockerfile.email-assistant` added
- [x] `/healthz` endpoints: DB ping + LLM ping — used by ACA health probes
- [x] Structured JSON logging (`python-json-logger`) with `trace_id` per line
- [x] Email assistant Container App added to ACA Terraform module
- [x] CI/CD pipeline (`ci.yml`) builds + deploys email_assistant image
- [x] `staging.tfvars` environment added
- [ ] **App policies loaded from Platform API** — `app.py` still hardcodes `PolicySet.from_yaml(...)`;
      `ao-platform/api/routes/policies.py` has full CRUD but email assistant doesn't read from it
- [ ] **Config-driven tracing** — switch `ManifestExecutor` to `LangfuseCallbackHandler`
      to auto-trace; remove manual span/generation calls from executor nodes
- [ ] **Policy evaluation Langfuse span** — policy check node runs silently post-execution

## Phase 4 — Platform Management ✅
*Goal: operators manage agents, tools, and policies from the AO Platform, not by editing files.*

- [x] **Tool registry API** — `POST /api/tools/`, `GET /api/tools/` backed by `ao_tools` DB table
- [x] **Manifest API** — `POST /api/apps/{app_id}/manifest` registers an app's manifest (mirror only;
      see ADR-008 for canonical ownership decision)
- [x] **Dashboard: Apps tab** — loads registered apps from `/api/apps/`; agent + tool drill-down;
      Upload Manifest modal
- [x] **App policies from API** — email assistant reads active policies from Platform at startup
- [x] **Dashboard UI overhaul** — Microsoft blue theme (#0078d4), no emoji, all stat numbers white
- [ ] **Tool access control** — `AgentConfig.tools` declared in manifest but `ManifestExecutor`
      does not enforce per-agent tool binding at runtime

## Phase 5 — Email Assistant Deepening ✅
*Goal: demonstrate true agent-driven tool use, supervisor orchestration, and visible reasoning.*

- [x] **LLM-driven DB lookup** — `lookup_taxpayer(tin)` registered as an LLM-callable tool
      via `executor.register_tool()`; LLM decides when to invoke it; Langfuse span
      `tool-lookup_taxpayer` nested inside each specialist generation; tool result detail
      (TIN + name) rendered as a purple sub-row in the UI workflow panel
- [x] **Supervisor/orchestrator pattern** — `_compile_supervisor()` + `_make_supervisor_node()`
      in `ManifestExecutor`; em-008 (Tan Boon Kiat Pte Ltd) uses `ao-manifest-supervisor.yaml`;
      supervisor routes `assessment_relief → payment_arrangement → FINISH`; outputs merged
      via LLM synthesis node; supervisor decisions pushed to SSE queue in real-time
- [x] **Token streaming** — specialists stream tokens to frontend via `asyncio.Queue`; live
      blue reply box with blinking cursor during generation; `type=token` SSE events
- [x] **Scratchpad reasoning visibility** — `show_reasoning: true` in manifest injects
      `<think>…</think>` instruction; extracted text emitted as `type=reasoning` SSE event;
      collapsible "Agent reasoning" accordion rendered in UI; ADR-009 documents scratchpad
      vs. native o1/o3/o4 reasoning tradeoffs
- [x] **ADR-009**: reasoning model strategy (scratchpad vs. native CoT)
- [x] **ADR-010**: orchestration pattern selection framework (router/concurrent/supervisor/linear)

## Phase 6 — Self-Hosted Langfuse on Azure
*Goal: data sovereignty — traces must not leave the Azure tenant.*

- [ ] Add `ca-langfuse-dev` Container App to `infra/modules/aca/main.tf`
      (`langfuse/langfuse:latest` image, internal ingress only)
- [ ] Wire to existing `psql-ao-dev` (separate `langfuse` database) and `redis-ao-dev`
      (Redis is already provisioned but unused — this is its first active use)
- [ ] Change `LANGFUSE_HOST` env var on email-assistant + ao-api from
      `https://cloud.langfuse.com` to the internal ACA FQDN
- [ ] Add Langfuse admin credentials to `secrets.auto.tfvars` + Key Vault
- [ ] Verify traces appear in self-hosted instance after `terraform apply`

## Phase 7 — Platform Hardening (open backlog)
*Goal: close real gaps between what is declared and what is enforced at runtime.*

### Messaging infrastructure
- [ ] **Service Bus wiring** — dead-letter handler (`workers/dead_letter.py`) exists but
      Service Bus queues are not connected to the live email processing path; wire
      `process_email_stream` failures to a dead-letter queue for retry + alerting
- [ ] **Redis usage** — Redis is provisioned (`redis-ao-dev`) but never read or written;
      intended use: SSE token queue persistence across ACA restarts, Langfuse worker queue

### Input/output validation
- [ ] **Pydantic tool schemas** — `register_tool()` accepts raw `dict` OpenAI function schema;
      replace with a `ToolSchema` Pydantic model that validates `name`, `description`,
      `parameters` at registration time; emit a typed error if schema is malformed
- [ ] **Agent-to-agent message validation** — messages passed between LangGraph nodes are
      untyped `dict`; introduce a `AgentMessage` Pydantic model for tool results,
      specialist outputs, and supervisor decisions; catches shape mismatches at node boundary
- [ ] **State schema enforcement** — `TaxEmailState` is a `TypedDict`; LangGraph does not
      enforce it at runtime; migrate to a Pydantic `BaseModel` state and configure
      `StateGraph(state_schema=TaxEmailState)` with strict validation

### Runtime enforcement
- [ ] **Tool access control per agent** — `AgentConfig.tools` is declared in manifest but
      `ManifestExecutor` passes all registered tools to every specialist; enforce that
      each specialist only receives tools listed in its manifest `tools:` field
- [ ] **Policy evaluation span** — policy check node runs post-execution silently;
      wrap in a Langfuse span so policy decisions are visible in traces
- [ ] **Config-driven tracing** — replace manual `lf_trace.generation(...)` calls in
      `ManifestExecutor` nodes with `LangfuseCallbackHandler`; reduces boilerplate and
      ensures all LLM calls are captured automatically

## Phase 8 — RAG Search Example
*Goal: validate manifest + linear pattern; second reference app.*

- [ ] `examples/rag_search` using the **linear** pattern
- [ ] pgvector embeddings or Azure AI Search
- [ ] `ao-manifest.yaml` declares linear agents + search tool
- [ ] Validates `ManifestExecutor` works across patterns (not just router + concurrent)

## Phase 9 — Graph Compliance Example
*Goal: validate supervisor pattern with user-delegated identity; third reference app.*

- [ ] `examples/graph_compliance` using the **supervisor** pattern
- [ ] Microsoft Graph API tool with user-delegated Entra identity
- [ ] Tests the `identity_mode: user_delegated` flow end-to-end
- [ ] Note: supervisor pattern now proven by em-008; this phase validates it against
      the Graph Compliance use case and user-delegated identity specifically

## Phase 10 — Azure Deployment (dev environment) ✅
*Goal: full stack running on Azure Container Apps (ACA) in a single dev environment.*

- [x] Provision `rg-ao-dev` and run `terraform apply -var-file=environments/dev.tfvars`
- [x] Store secrets in `kv-ao-dev` (OpenAI key, Langfuse keys, postgres password)
- [x] Build and push all three images to ACR; CI/CD (`ci.yml`) auto-deploys on push to `main`
- [x] `lifecycle { ignore_changes = [image] }` on all ACA apps — CI-deployed images
      no longer reverted by `terraform apply`
- [x] Smoke test: em-006 end-to-end against Azure-hosted stack
- [x] Langfuse Cloud traces verified for deployed runs

---

## Honest Gap Summary

| Capability | Declared | SDK | API | Dashboard | Enforced at runtime |
|---|---|---|---|---|---|
| Agent declaration (YAML) | ✅ | ✅ | — | — | ✅ |
| Tool declaration (YAML) | ✅ | ✅ | ✅ | ✅ | ❌ per-agent not enforced |
| Tool access per agent | ✅ | ✅ | ✅ | ✅ | ❌ all tools passed to all specialists |
| Tool input/output validation | ✅ schema declared | raw dict | — | — | ❌ no Pydantic validation |
| Agent message validation | — | — | — | — | ❌ untyped dict between nodes |
| Policy CRUD | ✅ | ✅ | ✅ | ✅ | ✅ loaded at startup |
| HITL approval flow | ✅ | ✅ | ✅ | ✅ | ✅ |
| Langfuse tracing | ✅ | ✅ | — | link | ✅ manual spans (not callback handler) |
| Service Bus (dead-letter) | ✅ handler exists | ✅ | — | — | ❌ not wired to live path |
| Redis | ✅ provisioned | — | — | — | ❌ not used |
| Scratchpad reasoning (CoT) | ✅ | ✅ `show_reasoning` | — | accordion UI | ✅ gpt-4.1-mini |
| Native reasoning (o1/o3/o4) | ADR-009 planned | — | — | — | ❌ not implemented |
