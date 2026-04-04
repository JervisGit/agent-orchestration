# ADR-015: Circuit Breaker and Rate Limiting for Agent Tool Calls

## Status
Proposed

## Context
Agents call external tools (e.g. the taxpayer DB owned by another team) as part of
their workflows. Two failure modes create risk:

1. **Tool call storms** — a stale or failed request triggers retries; a looping agent
   (LangGraph cycle, repeated tool invocations) can flood a downstream API with
   requests it did not expect and cannot absorb.
2. **Cascading failure** — if the downstream service is slow or returning errors, the
   agent keeps calling it, degrading the agent response and consuming credits/tokens.

The downstream team (taxpayer DB) is managed separately; our agents must not send
malicious or excessive requests to them. We are responsible for our own call hygiene.

Current state: no retry limit, no call-rate cap, no circuit breaker at the tool layer.

## Decision

Implement a **circuit breaker + per-run rate limit** in `ManifestExecutor._execute_tool_call`.

### 1. Per-run tool call limit
Each graph run holds a counter per tool name. If a tool is called more than
`max_calls_per_tool` times in a single run (default: 5, configurable in the manifest),
`_execute_tool_call` raises `ToolCallLimitExceeded` before making the actual call.

```yaml
# ao-manifest.yaml
tools:
  - name: lookup_taxpayer
    max_calls_per_run: 3   # override default of 5
```

### 2. Circuit breaker (per tool, process-wide)
A process-level `CircuitBreaker` object per registered tool tracks consecutive
failures. States: **Closed** (normal) → **Open** (calls blocked) → **Half-Open** (probe).

| Parameter | Default | Notes |
|---|---|---|
| `failure_threshold` | 5 | consecutive failures to open |
| `recovery_timeout` | 30 s | time in Open before moving to Half-Open |
| `half_open_max_calls` | 1 | probe calls allowed while Half-Open |

When the breaker is Open, `_execute_tool_call` returns a structured error immediately
(no HTTP request made): `{"error": "circuit_open", "tool": "lookup_taxpayer"}`.
The agent LLM sees this in its tool result and can gracefully degrade (e.g. proceed
without taxpayer data rather than looping).

### 3. Configurable backoff on retry
Tool retries (when the manifest declares `retry: true`) use **exponential backoff with
jitter** (base 1 s, max 8 s, ±20 % jitter). This prevents a thundering herd if many
concurrent runs all retry at the same moment after a transient failure.

### 4. Observability
Every circuit-state transition and every blocked call emits a structured log and a
Langfuse span with `circuit_state` tag, so the ops team can see at a glance when a
downstream service was unavailable and how many calls were blocked.

## Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| **Circuit breaker in executor (this ADR)** | Single enforcement point; works across all apps | Per-process state (not shared across ACA replicas) |
| Retry middleware in each tool function | Tool-specific control | Duplicated logic; no global visibility |
| Service mesh (Dapr, Istio) circuit breaker | Shared state across replicas; sidecar | Operational overhead; ACA doesn't natively support service mesh |
| Downstream rate-limit (APIM policy) | Shared across all callers | Only works for APIM-gated tools; doesn't prevent agent loops |

Shared-state circuit breaker (e.g. backed by Redis) is deferred until replica-scale
issues are observed; per-process is sufficient for current single-replica ACA deployment.

## Consequences
- `ManifestExecutor` gains a `_tool_call_counts` dict (per-run) and a process-level
  `_circuit_breakers` dict keyed by tool name.
- Manifest schema gains optional `max_calls_per_run` per tool entry.
- Agents must handle `{"error": "circuit_open"}` tool results gracefully — tested via
  the existing red-team suite (tool abuse tests in `TestPermissionBoundary`).
- Downstream teams (taxpayer DB) get protection without requiring changes on their side.
- Circuit breaker state is logged and traced; ops can configure alert thresholds in
  Langfuse or Azure Monitor.
