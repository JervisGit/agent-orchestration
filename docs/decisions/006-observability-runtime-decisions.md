# ADR-006: Runtime Observability & Streaming — Implementation Decisions

## Status
Accepted

## Context
During local development of the Tax Email Assistant demo, several concrete implementation
decisions were made that differ from what ADR-002 originally planned. This record documents
those choices, the constraints that drove them, and the implications for production.

---

## Decision 1: Langfuse Server v2, Python SDK pinned to `<3.0`

### What was decided
- Docker image pinned to `langfuse/langfuse:2` (not `:latest`) — deployed as an ACA container app, not cloud.langfuse.com
- Python SDK pinned to `langfuse>=2.0,<3.0` in `pyproject.toml`

### Why
Langfuse v3 requires **ClickHouse** as a mandatory dependency for its columnar analytics
storage. Standing up and operating ClickHouse adds significant infra complexity
(a separate stateful cluster, ~4 GB RAM minimum, separate backup/restore path).
For the current development phase this cost is not justified.

Langfuse v2 runs against PostgreSQL only — the same instance we already run for
application state — keeping the infra footprint minimal.

### Implications / risks
- When upgrading to Langfuse v3 (higher-volume production), a ClickHouse cluster must
  be provisioned and a one-time migration tool (`langfuse migrate`) must be run.
- The Python SDK version must be bumped in lock-step with the server version.
  SDK v4 **removed** the `.trace()` method entirely; using it against a v2 server
  raises `AttributeError` before any SSE data is sent, causing a silent client-side
  `ERR_INCOMPLETE_CHUNKED_ENCODING`. The pin prevents this regression.
- In `_create_langfuse_client()`, the `.trace()` call is wrapped in `try/except` so
  a future SDK/server mismatch degrades gracefully rather than silently killing streams.

### Upgrade path
```
# Future: when upgrading to v3
docker pull langfuse/langfuse:3
# Must also provision ClickHouse before upgrading
pip install "langfuse>=3.0,<4.0"
```

---

## Decision 2: SSE (Server-Sent Events) for real-time step streaming

### What was decided
The `/api/emails/{id}/process/stream` endpoint uses **HTTP SSE** (`text/event-stream`)
via FastAPI `StreamingResponse` with an `async` generator over `compiled_graph.astream()`.

### Why
SSE is the simplest choice for one-way server→client streaming when the client is a
browser with no existing WebSocket connection:

| Option | Pros | Cons |
|--------|------|------|
| **SSE** | Native browser `EventSource` API, auto-reconnect, works over plain HTTP/1.1, no extra infra | One-directional only |
| WebSockets | Bidirectional, lower overhead for high-frequency messages | Requires WS upgrade handshake, more complex client code |
| Long-polling | Works everywhere | High overhead, one round-trip per update |
| Batch (POST+poll) | Simplest server | Latency, polling overhead |

For displaying agent step progress (unidirectional, low frequency ~3–6 events per request)
SSE is the right fit.

### HTTPS in production
In production (Azure Container Apps / AKS Ingress), SSE works identically over HTTPS/TLS.
The `EventSource` client will use `https://` automatically when the page is served over
HTTPS. No code changes are needed. The `X-Accel-Buffering: no` response header is already
set to disable nginx proxy buffering, which is required for SSE to work behind a reverse
proxy (including AGIC / nginx ingress on AKS).

### Resilience note
A known browser behaviour: `EventSource.onerror` fires on **both** true errors and on
the normal connection close after the server sends the final event and closes the stream.
The frontend guards this with a `streamCompleted` flag that is set in the `complete`
handler before `source.close()` is called.

---

## Decision 3: What is explicitly traced / logged in Langfuse

Every email processing run creates a **trace** containing the following spans and
generations. These are all **explicitly instrumented** in `app.py` — nothing is
auto-instrumented by Langfuse.

| Langfuse entry | Type | What it captures |
|---|---|---|
| `process-email` | **Trace** (root) | Full email text as input, metadata: email_id, sender, subject |
| `db-lookup-taxpayer` | **Span** | Input: sender email + TIN (if extracted). Output: `found: true/false`, tax_id |
| `classify` | **Generation** | Input: classifier system prompt + email. Output: category string. Token usage |
| `specialist-{category}` | **Generation** | Input: SOP-grounded system prompt + taxpayer context + email. Output: draft reply. Token usage. `sop_applied: true` metadata |

The top-level trace is also updated at the end with `output` (the draft reply) and
`metadata` containing `category`, `hitl`, and `policy_flags`.

**Not yet traced** (known gaps):
- The policy engine evaluation pass (post-execution guardrail check) — currently runs
  silently; no span is created for it
- HITL escalation events
- Token costs (usage numbers are captured but cost mapping is not configured in Langfuse)

---

## Decision 4: Policy / guardrail enforcement

Three policies are registered inline in `app.py` against the AO `PolicyEngine`:

| Policy name | Stage | Action | What it checks |
|---|---|---|---|
| `pii_filter` | `post_execution` | `redact` | PII patterns in the draft reply |
| `content_safety` | `post_execution` | `warn` | Harmful or inappropriate content |
| `tax_accuracy` | `post_execution` | `warn` | Tax-specific accuracy heuristics |

These are evaluated against the draft reply after the specialist agent finishes. The
`penalty_waiver` node also adds a hard-coded **HITL flag** (not a policy) when
`penalty_count >= 3`, reflecting SOP rule 1.3 that waiver decisions for repeat offenders
require supervisor approval.

**Production gap**: policies are defined inline in Python. For production they should be
loaded from the database (`ao_policies` table) or from `ao-manifest.yaml` so operators
can adjust them without a code deploy. See the `ao-platform` Policies page for the DB
schema already in place.

---

## Decision 5: Logging and debuggability

### Current state (local dev)
- uvicorn outputs to stdout/stderr of the terminal process
- `logger = logging.getLogger("tax_email_assistant")` — log level follows uvicorn default (`info`)
- The SSE generator logs `INFO` on start/complete and `EXCEPTION` on error

### Why the SDK v4 bug was hard to catch
The crash happened inside an **async generator before the first `yield`**. FastAPI had
already sent the HTTP 200 + chunked headers, so the browser saw a valid response that
then abruptly closed — showing only `ERR_INCOMPLETE_CHUNKED_ENCODING` with no body.
The server-side `AttributeError` was printed to the uvicorn stderr stream, but that
stream was mixed with stdout in the terminal and was easy to miss.

### What should be done for production
1. **Structured JSON logging** — switch to `python-json-logger` or structlog so every
   log line is a JSON object with `trace_id`, `email_id`, `level`, `timestamp`. This
   makes log aggregation (Azure Log Analytics / ELK) trivial.
2. **Centralised log sink** — in ACA/AKS, container stdout/stderr is automatically
   shipped to Azure Monitor if the Log Analytics workspace is wired to the container
   environment (already in the Terraform `observability` module).
3. **Sentry or Azure Application Insights** for exception capture — unhandled
   exceptions in async generators would appear in the dashboard immediately.
4. **Health endpoint** — `/healthz` should report LLM provider ping, DB ping, and
   Langfuse ping so infra probes catch partial-start failures.

### On Azure Container Apps specifically
- All `stdout`/`stderr` from the container is automatically forwarded to the Log
  Analytics workspace linked to the Container Apps Environment.
- Query with: `ContainerAppConsoleLogs_CL | where ContainerAppName_s == "ao-email-assistant"`
- This means the silent stderr error **would** have been visible in Azure Monitor logs
  even without structured logging. Locally we just lacked a persistent log file.

---

---

## Decision 6: Tracing strategy — explicit per-app vs config-driven

### Current approach (explicit)
Each app manually calls `lf_trace.span()` / `lf_trace.generation()` in its node
functions. Metadata like `sop_applied: true` and taxpayer context are attached by hand.

### Why config-driven is the right direction (but not yet)
When there are many apps, every app team re-implementing the same span/generation
boilerplate is wasteful and inconsistent. The better pattern:

1. **Auto-tracing via callback** — Langfuse ships a `LangfuseCallbackHandler` for
   LangGraph. Passing it to `compiled_graph.astream(..., config={"callbacks": [handler]})`
   auto-traces every node entry/exit, latency, and token counts with ~5 lines of code.
   This covers the structural tracing (what ran, how long, how many tokens).

2. **Business metadata via manifest** — App-specific metadata (`sop_applied`, taxpayer
   fields, category) should come from the manifest's `agents[].trace_metadata` config
   rather than hardcoded Python. The AO engine then merges this into each span.

### Why not now
- Only one app exists today; the callback handler approach would save ~40 lines but
  isn't blocking anything.
- Manifest-driven trace metadata requires the config-driven agent architecture (ADR-007)
  to be built first — the metadata has nowhere to live until agents are manifest-declared.
- Effort: switching to the callback alone is low (~5 lines); the full manifest-driven
  metadata approach depends on ADR-007 scope.

### Recommendation
When implementing ADR-007 (config-driven agents), add `trace_metadata` as a first-class
field on `AgentConfig` and switch to `LangfuseCallbackHandler` for auto-tracing. Keep
explicit `.generation()` calls only for cases where you need to override the default
span structure.

---

## Consequences

- Langfuse v2 is a deliberate short-term choice; upgrade path to v3 is documented above.
- SSE is production-ready as-is; no changes needed for HTTPS on ACA/AKS.
- Structured logging and a `/healthz` endpoint are pre-production requirements.
- Policies, SOPs, and tracing metadata are currently hardcoded — addressed together in ADR-007.
- Config-driven tracing via `LangfuseCallbackHandler` is low-effort but deferred until
  ADR-007 defines the agent manifest structure.
