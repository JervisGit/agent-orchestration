# ADR-016: Tools Registry and MCP Server

**Status:** Proposed  
**Date:** 2025-07-01

---

## Context

Currently each application manually registers tools at startup:

```python
# examples/email_assistant/backend/app.py
executor.register_tool("lookup_taxpayer", _tool_lookup_taxpayer, _LOOKUP_TAXPAYER_SCHEMA)
```

`ManifestExecutor.register_tool()` delegates to an in-process `ToolRegistry` that is local to each executor instance. There is no central catalog, no schema versioning, and no visibility across agents or apps.

Two concerns have emerged:

1. **Tool duplication** — `lookup_taxpayer` is registered in both the standard and supervisor executors in the same app. If the same tool were needed in a second app, the implementation and JSON schema would have to be copied.
2. **Tool authority** — AO platform has no way to audit what tools are in use, enforce schema versions, or apply circuit-breaker configs (ADR-015) consistently without in-process coupling.

---

## Decision

### Part A — Platform-level Tools Registry

Introduce a lightweight platform registry stored in the AO PostgreSQL database (table `ao_tool_catalog`) alongside the existing state tables.

| Column | Purpose |
|---|---|
| `tool_name` | Unique identifier (e.g. `lookup_taxpayer`) |
| `schema_version` | Semver string |
| `json_schema` | OpenAI-compatible function-calling schema |
| `owner_app` | App that owns the implementation |
| `mcp_endpoint` | Optional — URL of the MCP server if the tool is centralised |

Apps continue to call `executor.register_tool()` at startup, but `ManifestExecutor` will optionally validate the provided schema against the catalog entry. This is non-breaking: apps that do not register a catalog entry retain current behaviour.

### Part B — MCP Server (when justified)

An MCP (Model Context Protocol) server is warranted when **any of the following** apply:

- The tool is shared across two or more independent apps or agents.
- The tool requires a DB connection that should not be replicated into every app pod (e.g. taxpayer DB, internal CRM).
- The tool implementation needs independent scaling, versioning, or audit logging.

For the email assistant, `lookup_taxpayer` touches the internal taxpayer DB and is a candidate for centralisation once a second consumer emerges.

#### Architecture

```
Agent Pod (ACA)
  └─ ManifestExecutor
       └─ HTTP POST ──► APIM ──► MCP Server Pod (ACA)
                                      └─ UAMI ──► Azure PostgreSQL (taxpayer DB)
```

- **MCP Server**: A lightweight FastAPI service hosted as a separate ACA container. Exposes tools under `/tools/{tool_name}` following the MCP spec.
- **Transport**: HTTPS request-response (standard ACA service-to-service). SSE streaming is used only if the tool response is long-running; otherwise plain HTTP POST/response is sufficient. No WebSocket is needed for synchronous tool calls.
- **Authentication**: Agents authenticate to the MCP server via APIM (same managed-identity pattern as ADR-013). The MCP server authenticates to the DB via UAMI — no credentials in application config.
- **Circuit breaker**: The per-tool `CircuitBreaker` (ADR-015) in `ManifestExecutor` wraps the outbound HTTP call to the MCP endpoint the same way it wraps local tool calls.

#### MCP endpoint contract

```
POST /tools/lookup_taxpayer
Authorization: Bearer <managed-identity token>
Content-Type: application/json

{ "tin": "SG-T001-2890" }

→ 200 { "name": "...", "entity_type": "...", ... }
→ 404 { "error": "taxpayer_not_found" }
→ 503 (circuit-breaker open on MCP side)
```

---

## Alternatives Considered

| Option | Verdict |
|---|---|
| Keep all tools in-process per app | ✅ Current approach; viable while tools are not shared |
| APIM + Azure Functions per tool | Higher cold-start latency; more infra to manage |
| Dapr service invocation | Adds sidecar overhead; not justified at current scale |

---

## Consequences

- No immediate change required; current `register_tool()` approach remains default.
- When a second app needs `lookup_taxpayer`, extract it to an MCP server container and update `mcp_endpoint` in `ao_tool_catalog`. No change to consuming app code — only the tool registration changes from a local function to an HTTP call wrapper.
- MCP server is a new ACA container and ACA app in Terraform (`modules/aca/`); add when the first shared-tool candidate is confirmed.
