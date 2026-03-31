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
  - `concurrent` pattern (was `magentic`): detect all intents → parallel dispatch
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

## Phase 3 — Production Readiness
*Goal: safe to deploy, observable in Azure.*

- [x] HITL escalation events traced (resolution event on originating Langfuse trace) ✅
- [x] Container images updated to Python 3.13, correct runtime deps ✅
- [x] `Dockerfile.email-assistant` added ✅
- [x] `/healthz` endpoints: DB ping + LLM ping — used by ACA health probes ✅
- [x] Structured JSON logging (`python-json-logger`) with `trace_id` per line ✅
- [x] Email assistant Container App added to ACA Terraform module ✅
- [x] CI/CD pipeline (`ci.yml`) builds + deploys email_assistant image ✅
- [x] `staging.tfvars` environment added ✅
- [ ] Config-driven tracing: switch `ManifestExecutor` to `LangfuseCallbackHandler`
      for auto-tracing; remove manual span/generation calls from executor nodes
- [ ] Policy evaluation Langfuse span (currently silent in post-execution check)

## Phase 4 — RAG Search Example
*Goal: validate manifest + linear pattern; second reference app.*

- [ ] `examples/rag_search` using the **linear** pattern
- [ ] pgvector embeddings or Azure AI Search
- [ ] `ao-manifest.yaml` declares linear agents + search tool
- [ ] Validates `ManifestExecutor` works across patterns (not just router)

## Phase 5 — Graph Compliance Example
*Goal: validate supervisor pattern; test user-delegated identity.*

- [ ] `examples/graph_compliance` using the **supervisor** pattern
- [ ] Microsoft Graph API tool with user-delegated Entra identity
- [ ] Tests the `identity_mode: user_delegated` flow end-to-end

## Phase 6 — Azure Deployment
*Goal: full end-to-end on Azure Container Apps.*

- [ ] Real container images replacing placeholder images in ACR
- [ ] CI/CD pipeline triggers on main branch (`infra/` + `ao-core/` + `ao-platform/`)
- [ ] End-to-end smoke test against ACA after deploy
- [ ] Production environment (`prod.tfvars`) — gated by manual approval

---

## Open Items (pre-production)
See `docs/decisions/006-observability-runtime-decisions.md` for full detail.

- Policies + SOPs loaded from DB / manifest (not hardcoded Python)
- Config-driven Langfuse tracing via `LangfuseCallbackHandler`
- Policy evaluation Langfuse span
