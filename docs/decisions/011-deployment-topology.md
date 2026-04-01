# ADR-011: Deployment Topology — Modular Monolith, Not Microservices

## Status

Accepted

## Date

2026-04-01

## Context

The AO email assistant runs multiple agents (classifier, specialists, supervisor) that
coordinate to process a single email. There are two broad deployment topologies:

**Modular monolith** — all agents run as functions inside one process. Coordination
happens in-process via LangGraph state (Python dicts passed between node functions).
One container image, one deployed unit.

**Microservices** — each agent runs as its own service (container/pod). Coordination
happens over the network via HTTP, gRPC, or a message queue (e.g. Service Bus).
Each agent has its own deployment lifecycle and can be scaled independently.

The decision affects: latency, ops complexity, failure isolation, scaling granularity,
and the role of Service Bus and Redis in the architecture.

## Current architecture (modular monolith)

All agents are functions compiled into a single LangGraph graph by `ManifestExecutor`.
The graph runs inside one `uvicorn` process on `ca-email-assistant-dev`.

```
┌─────────────────────────── ca-email-assistant-dev (1 container) ──────────────┐
│                                                                                │
│  FastAPI request                                                               │
│       │                                                                        │
│       ▼                                                                        │
│  LangGraph graph (in-process)                                                  │
│  ┌──────────────┐   ┌────────────────────┐   ┌───────────────────────────┐    │
│  │  classifier  │──▶│  specialist agents │──▶│  supervisor / merge node  │    │
│  └──────────────┘   │  (parallel or seq) │   └───────────────────────────┘    │
│                     └────────────────────┘                                    │
│  State passed between nodes as Python dict — zero network hops                │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘
         │                              │
      PostgreSQL                      Redis
  (taxpayer lookup,              (checkpoints +
   HITL persistence)              processed state)
```

Service Bus is used for failure notification only (dead-letter on stream crash),
**not** for agent-to-agent communication.

## Decision

**Maintain the modular monolith** for the current and near-term phases.

The modularity is preserved at the code level — agents are declared in YAML manifests,
`ao-core` ships as a separate package, patterns are swappable — but at runtime they
execute in a single process.

## Rationale

### Why monolith fits now

| Factor | Reasoning |
|---|---|
| **Latency** | em-008 (supervisor pattern) takes ~24s total — almost entirely LLM API time, not agent handoff time. Adding a network hop per node would add 10–100ms per step with no benefit. |
| **Ops complexity** | One container to deploy, monitor, health-check, and scale. Microservices would require N health checks, service discovery, distributed tracing across service boundaries, and a service mesh or API gateway. |
| **Failure model** | LLM calls are the dominant failure mode. A specialist agent failing is handled by the tool-calling loop and HITL escalation — not by pod restarts. |
| **Team size** | The overhead of maintaining independent CI/CD pipelines, versioning contracts between services, and managing inter-service API compatibility is not justified. |
| **State complexity** | LangGraph state (specialist_outputs, messages, taxpayer, hitl_required) is a rich shared dict. Externalising this to a message queue per hop would require serialising/deserialising on every node boundary, adding latency and fragility. |

### What scaling looks like right now

Scaling is horizontal at the **container** level — ACA scales out `ca-email-assistant-dev`
replicas, each running the full monolith. Every replica can handle any email independently.

This means:
- You cannot scale just the `assessment_relief` agent because it receives 10x more traffic
  than `penalty_waiver`.
- Memory and CPU for all agents grows together, even if only one agent is under load.

For the current load profile (demo, low concurrency, LLM-bound not CPU-bound) this is
not a real constraint.

### What would force a switch to microservices

The following conditions would justify decomposing into per-agent services:

1. **Significantly uneven agent load** — one specialist handles 80%+ of requests and
   becoming the bottleneck while others sit idle. The cost of independent scaling starts
   exceeding the cost of ops complexity.

2. **Independent deployment cadence** — different teams own different agents and need to
   deploy them on separate schedules without coordinating a monolith release.

3. **Agent resource profiles diverge significantly** — e.g. one agent runs a local model
   and needs GPU; others need only CPU. Packing them into one container wastes resources.

4. **Fault isolation requirement** — a bug in one specialist must not affect others.
   Currently a crash in the `penalty_waiver` node can bring down the whole graph for
   that request.

5. **Latency is no longer LLM-bound** — if agent handoff latency (not LLM call time)
   becomes the bottleneck, in-process coordination has already been optimised as far as
   it can go.

## Migration path if decomposition is required

The architecture is intentionally designed to make this migration tractable:

1. `ao-core` is already a separate pip-installable package — each agent service would
   import it independently.

2. `AgentConfig` is fully serialisable (dataclass + YAML) — agent definitions can be
   distributed to separate services without code changes.

3. Service Bus is already provisioned (`sb-ao-dev`, `ao-workflow-events` topic) — the
   `_make_specialist_node` in `ManifestExecutor` would be replaced with a Service Bus
   publisher; each agent service would subscribe and publish results back.

4. Redis is already provisioned and used for state — the shared LangGraph state dict
   would move to a Redis-backed store between service hops.

5. The `ManifestExecutor` compile step would generate a *distributed* graph where each
   node is a remote call rather than an in-process function — the manifest YAML would
   not need to change.

## Consequences

- **Scaling is coarse-grained** — the entire email assistant scales together, not per
  agent. Accepted for now.
- **No fault isolation between agents** — a specialist crash affects the current request.
  Mitigated by HITL escalation and dead-letter on stream failure.
- **State remains in-process during execution** — suitable for the current single-tenant
  demo. Would need externalising before multi-tenant or high-concurrency production use.
- **Clear migration trigger criteria** defined above — the decision to decompose will be
  driven by measurable conditions, not speculation.
