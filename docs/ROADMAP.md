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

## Phase 4 — Platform Management
*Goal: operators manage agents, tools, and policies from the AO Platform, not by editing files.*

The SDK already has the right structures; this phase wires them end-to-end:

- [ ] **Tool registry API** — `POST /api/tools/`, `GET /api/tools/` backed by `ao_tools` DB table;
      `ToolRegistry` in `ao-core` already handles registration but has no HTTP surface
- [ ] **Tool access control** — `AgentConfig.tools: [tool_names]` is declared in manifest
      but `ManifestExecutor` does not enforce it; add per-agent tool binding in executor
- [ ] **Manifest API** — `POST /api/apps/{app_id}/manifest` to register/update an app's
      `ao-manifest.yaml` via HTTP (store + validate in DB); currently only file-based
- [ ] **Dashboard: Apps tab** — show registered apps, agents per app, tools per agent;
      currently the "DSAI Apps" nav item exists but has no content
- [ ] **App policies from API** — email assistant (and all future apps) reads active policies
      from `GET /api/policies?app_id=` at startup instead of hardcoding them

## Phase 5 — RAG Search Example
*Goal: validate manifest + linear pattern; second reference app.*

- [ ] `examples/rag_search` using the **linear** pattern
- [ ] pgvector embeddings or Azure AI Search
- [ ] `ao-manifest.yaml` declares linear agents + search tool
- [ ] Validates `ManifestExecutor` works across patterns (not just router + concurrent)

## Phase 6 — Graph Compliance Example
*Goal: validate supervisor pattern; test user-delegated identity.*

- [ ] `examples/graph_compliance` using the **supervisor** pattern
- [ ] Microsoft Graph API tool with user-delegated Entra identity
- [ ] Tests the `identity_mode: user_delegated` flow end-to-end

## Phase 7 — Azure Deployment (dev environment)
*Goal: full stack running on Azure Container Apps (ACA) in a single dev environment.*

- [ ] Provision `rg-ao-dev` and run `terraform apply -var-file=environments/dev.tfvars`
- [ ] Store secrets in `kv-ao-dev` (OpenAI key, Langfuse keys, postgres password)
- [ ] Build and push all three images to ACR; CI/CD auto-deploys on push to `main`
- [ ] Smoke test: process em-006 end-to-end against Azure-hosted stack
- [ ] Verify Langfuse Cloud traces are visible for the deployed run

---

## Honest Gap Summary

| Capability | Declared | SDK | API | Dashboard | Enforced at runtime |
|---|---|---|---|---|---|
| Agent declaration (YAML) | ✅ | ✅ | — | — | ✅ |
| Tool declaration (YAML) | ✅ | ✅ | ❌ | ❌ | ❌ *not wired* |
| Tool access per agent | ✅ | ✅ | ❌ | ❌ | ❌ *not enforced* |
| Policy CRUD | ✅ | ✅ | ✅ | ✅ | ❌ *apps hardcode* |
| HITL approval flow | ✅ | ✅ | ✅ | ✅ | ✅ |
| Langfuse tracing | ✅ | ✅ | — | link | ✅ *manual spans* |
