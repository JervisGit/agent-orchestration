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

## Phase 2 — Manifest-Driven Engine ✅ (this session)
*Goal: app teams declare agents + SOPs in YAML; no LangGraph code in app repos.*

- ADR-007: config-driven agents decision
- `AgentConfig`: add `sop`, `hitl_condition`, `trace_metadata` fields
- `AppManifest`: add `pattern`, `classifier_agent`, `intent_agents` fields
- `ManifestExecutor`: reads `ao-manifest.yaml`, builds the LangGraph graph, manages
  Langfuse trace lifecycle — apps never import `StateGraph` or `END`
  - `router` pattern: classify → one specialist → END
  - `magentic` pattern: detect all intents → parallel dispatch via `asyncio.gather`
    → LLM merge → END (multi-intent emails get full coverage from every specialist)
- Pattern library: `router`, `linear`, `supervisor`, `planner`, `magentic`
- Email assistant refactored: `app.py` uses `ManifestExecutor`; LangGraph dependency
  isolated entirely inside `ao-core`
- `ao-manifest.yaml`: full declaration of 5 specialist agents with SOPs + policies
- Sample emails updated: em-006 (Fatimah penalty waiver → HITL demo),
  em-007 (multi-intent filing + payment → magentic pattern demo)

## Phase 3 — Production Readiness
*Goal: safe to deploy, observable in Azure.*

- [ ] Structured JSON logging (`python-json-logger`) with `trace_id` on every line
- [ ] `/healthz` endpoint: LLM ping, DB ping, Langfuse ping — used by ACA health probes
- [ ] Config-driven tracing: switch `ManifestExecutor` to `LangfuseCallbackHandler`
      for auto-tracing; remove manual span/generation calls from executor nodes
- [ ] Policy evaluation Langfuse span (currently silent in post-execution check)
- [ ] HITL escalation events traced
- [ ] Staging environment (Terraform `staging.tfvars`)

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

## Open Items (pre-production blockers)
See `docs/decisions/006-observability-runtime-decisions.md` for full detail.

- Structured JSON logging
- `/healthz` endpoint
- Policies + SOPs loaded from DB / manifest (not hardcoded Python)
- Config-driven Langfuse tracing via `LangfuseCallbackHandler`
