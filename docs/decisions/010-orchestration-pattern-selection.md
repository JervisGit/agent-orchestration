# ADR-010: Orchestration Pattern Selection

## Status
Accepted

## Context
`ManifestExecutor` supports four compiled patterns — `router`, `concurrent`, `supervisor`,
and `linear` — each with different tradeoff profiles. A given use case (e.g. the tax email
assistant) can be legitimately implemented with more than one pattern. We need a documented
decision framework so that:

1. App teams choose the right pattern for their use case from the start.
2. AO platform owners understand when to add a new pattern vs. reuse existing ones.
3. The decision of *who decides which pattern to use* (hardcoded in manifest vs. LLM-selected
   at runtime) is explicit and auditable.

## Decision

### Pattern selection is declared in `ao-manifest.yaml`, not determined at runtime

An AO application's `pattern` field is set by the app team at design time and hardcoded
in the manifest. The platform does **not** have an LLM agent that inspects an incoming
request and decides which pattern to execute.

**Rationale:**
- **Auditability** — regulators and reviewers can read the manifest and know exactly what
  control flow is possible. A dynamic pattern selector produces non-deterministic routing
  that is harder to explain.
- **Cost predictability** — each pattern has a known LLM call budget per request. A
  meta-agent that dynamically selects patterns adds an unbounded outer loop.
- **Blast radius** — if the pattern-selector LLM hallucinates an invalid pattern, every
  request fails. Pattern-specific failures are contained to that pattern.
- **Simplicity** — most use cases have a clear best-fit pattern. The decision framework
  below handles the ambiguous cases.

The one exception is the **supervisor** pattern itself: its orchestrator LLM dynamically
selects which *specialist* to invoke next. But the set of eligible specialists and the
outer loop termination condition are declared statically in the manifest — the LLM cannot
invent a new specialist or run indefinitely.

---

### Pattern decision framework

```
Is the email/request always handled by exactly one domain?
    YES → router
    NO  ↓

Are all domains independent (can run in parallel without needing each other's output)?
    YES → concurrent (fan-out + LLM merge)
    NO  ↓

Does the order of specialist invocations matter, or does one specialist's output
inform the next specialist's work?
    YES → supervisor (sequential orchestrator loop)
    NO  → concurrent (still fine; order doesn't matter)

Is the workflow a strict fixed pipeline (step A always precedes step B)?
    YES → linear
```

| Pattern | LLM calls / request | Latency | When to use |
|---|---|---|---|
| `router` | 1 (classify) + 1 (specialist) | Fastest | Single domain per request. Most email triage. |
| `concurrent` | 1 (classify) + N (specialists, parallel) + 1 (merge if N>1) | Fast | Multi-intent requests where specialists are independent. |
| `supervisor` | K×1 (supervisor) + K×1 (specialist) + 1 (merge if K>1) | Slowest | Multi-step cases where each specialist's output should guide the next. Sequential compliance checks. |
| `linear` | N each in sequence | Medium | Fixed pipeline: extract → enrich → validate → respond. RAG search. |

---

### Email assistant pattern choice: concurrent for em-001…007, supervisor for em-008

The email assistant uses **both** patterns intentionally to demonstrate the difference:

- **em-001…007** use the `concurrent` pattern (`ao-manifest.yaml`): an intent classifier
  detects all applicable categories upfront, then all matched specialists run in parallel.
  For single-intent emails (the majority) this degenerates to `classify → 1 specialist → merge`.
  The merge step is a no-op for single intents.

- **em-008** uses the `supervisor` pattern (`ao-manifest-supervisor.yaml`): the orchestrator
  reads the email, routes to `assessment_relief`, reviews the output, then routes to
  `payment_arrangement`, then decides FINISH. The order matters — the payment specialist
  knows from context that an objection is already in progress.

**Why not always use supervisor?** For em-001 (simple filing extension, one domain), supervisor
would be `supervisor→filing_extension→supervisor(FINISH)` — two extra LLM calls for no benefit.
For em-007 (two independent intents), supervisor would run the specialists sequentially when
they could run in parallel. The 24s trace time on em-008 vs ~6s on em-001 illustrates the cost.

---

### Open question: per-request pattern selection

A future capability would be a meta-agent that reads an incoming request and routes it to
the correct executor instance (concurrent vs. supervisor). This is the "should AO agents
decide which pattern to use?" question. Current position:

- **Not implemented.** Each email in `emails_db` has an optional `"mode": "supervisor"` flag
  set statically in `app.py`. The SSE endpoint checks this and routes to `executor_sv`.
- **If added**, it would be a pre-processing step that classifies the request *complexity*
  (single-intent / multi-intent-parallel / multi-intent-sequential) and selects the executor.
  This is itself a classification problem — implementable as a `router` pattern calling a
  complexity classifier, not a free-form LLM decision.
- **Governance concern:** dynamic pattern selection must be logged (which pattern was chosen
  and why) and bounded (a fixed allowlist of valid patterns). Unbounded meta-orchestration
  is out of scope for the current platform version.

## Consequences
- App teams must declare `pattern` in their manifest and justify it against this framework.
- `ManifestExecutor.compile()` rejects unknown pattern names at startup — fail-fast.
- Apps that need both patterns for different request types (like the email assistant) must
  instantiate two executors and route between them in app code, not in the manifest.
  This is a deliberate constraint — the manifest describes one workflow, not a branching meta-workflow.
- The framework table above should be referenced in new app onboarding documentation.
