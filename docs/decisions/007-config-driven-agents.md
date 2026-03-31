# ADR-007: Config-Driven Agents, Tools, and SOPs via ao-manifest.yaml

## Status
Accepted

## Context
The email assistant demo (Phase 1) built its LangGraph workflow directly in `app.py`:
importing `StateGraph`, calling `add_node`/`add_edge`, and embedding SOPs as Python
string literals. This creates three problems:

1. **Replication** — every new app rewrites the same LangGraph boilerplate.
2. **Framework coupling** — if LangGraph is replaced (CVE, deprecation, better alternative),
   every app repo must change, not just `ao-core`.
3. **Ops opacity** — SOPs and agent configurations live in Python source; operators
   cannot inspect or adjust them without modifying code and redeploying.

## Decision
Introduce `ManifestExecutor` in `ao-core`: it reads an `ao-manifest.yaml` file and
builds the LangGraph graph automatically. App code never imports `StateGraph` or `END`.

App teams only write:
- `ao-manifest.yaml` — agent names, system prompts, SOPs, policies, HITL conditions
- Truly app-specific async functions — DB lookups, custom state schemas, FastAPI routes

### What moves into `ao-manifest.yaml`
```yaml
pattern: router               # router | linear | supervisor | planner
classifier_agent: classifier  # which agent routes

agents:
  - name: classifier
    system_prompt: "Classify into: {categories}"   # {categories} auto-filled
    temperature: 0.0

  - name: filing_extension
    system_prompt: "You are a tax officer handling filing extensions..."
    sop: |
      SOP — Filing Extension
      1. Check deadline not already passed.
      ...
    temperature: 0.2
    hitl_condition: null      # optional Python expression against state

policies:
  - name: pii_filter
    stage: post_execution
    action: redact
```

### What `ManifestExecutor` does
1. Parses the manifest and identifies the classifier agent and specialist agents.
2. Auto-builds a LangGraph matching the declared `pattern`:
   - **router**: `pre_steps → classifier → specialist_{route} → END`
   - (future) **linear**, **supervisor**, **planner** via existing pattern builders
3. Auto-generates classifier node: calls LLM with system_prompt; injects `{categories}`
   placeholder with specialist names; validates response against valid categories.
4. Auto-generates specialist nodes: calls LLM with `system_prompt + SOP`; prepends
   `state["_context"]` (set by app pre-steps, e.g. taxpayer record) to system prompt.
5. Evaluates `hitl_condition` (Python expression) after specialist runs; sets
   `state["hitl_required"]` and appends to `state["policy_flags"]` if truthy.
6. Manages Langfuse trace lifecycle per `astream()`/`ainvoke()` call:
   opens a trace, attaches generations for classifier + each specialist, closes on finish.
7. Exposes `get_trace(trace_id)` so app pre-steps (like DB lookup) can attach custom spans.

### Convention: `_context` state key
Pre-steps that want to inject contextual text into specialist system prompts set
`state["_context"]` (a plain string). The executor prepends this to every specialist's
system prompt. This decouples the DB schema from the executor.

### Pre-steps registration
```python
executor = ManifestExecutor(manifest, llm, langfuse_client=lf)
executor.register_pre_step("lookup_taxpayer", node_lookup_taxpayer)
compiled_graph = executor.compile(state_schema=TaxEmailState)
```

## Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| **ManifestExecutor (chosen)** | Apps decoupled from LangGraph; single migration point | Executor adds indirection; complex patterns may need escape hatch |
| Keep LangGraph in app.py | Full flexibility | Defeats AO abstraction; upgrade/CVE sprawl |
| Code-gen from manifest | Fully declarative | Too complex, hard to debug generated code |
| Callback-only (no executor) | Minimal change | Doesn't remove LangGraph from app repos |

## Tracing approach (deferred full config-driven tracing)
`ManifestExecutor` handles trace open/close and attaches generations per agent node.
Config-driven tracing via `LangfuseCallbackHandler` (auto-instrumenting all nodes) is
deferred until Phase 3 because it requires deciding how to map callback spans to
business metadata from the manifest's `trace_metadata` per-agent config.

## Consequences
- Apps have zero LangGraph imports — framework migration is a one-file change in `ao-core`.
- SOPs are in YAML → editable by ops team; searchable; diffable in git.
- `ManifestExecutor` must support all four patterns eventually (router done; linear,
  supervisor, planner follow same model using existing `ao/engine/patterns/` builders).
- Complex apps with non-standard graphs can still use `LangGraphEngine.register_graph()`
  directly as an escape hatch.
- `hitl_condition` uses Python `eval()` against a restricted namespace; acceptable for
  developer-authored manifests. Future hardening: replace with a simple DSL or enum.
