# ADR-019: PostgreSQL-Backed Agent-to-Agent State (LangGraph Checkpointer)

## Status
Accepted

## Context

All agent-to-agent communication inside a ManifestExecutor run flows through
LangGraph graph state.  Before this ADR, `MemorySaver` (in-process Python dict)
was the sole checkpointer, meaning:

- State was lost on any process restart (ACA revision rollout, scaling-in, crash).
- No external observability into what messages passed between agents mid-run.
- Cross-run replay was impossible.

`AsyncRedisSaver` was already ruled out (ADR-003 consequence): Azure Cache for
Redis Basic/Standard does not load the `RedisJSON` module required by the
LangGraph Redis checkpointer.

## Decision

Replace `MemorySaver` with `AsyncPostgresSaver` from the
`langgraph-checkpoint-postgres` package, using the same PostgreSQL Flexible Server
already in the stack (ADR-003).

The switch is opt-in at app level: each app calls
`await executor.setup_pg_checkpointer(DATABASE_URL)` in its FastAPI lifespan
**before** `compile()`.  If the database is unavailable (e.g. local dev without
Docker), the setup call fails gracefully and the app falls back to `MemorySaver`.

## What is persisted

LangGraph creates three tables automatically via `checkpointer.setup()`:

| Table | Contains |
|---|---|
| `checkpoints` | One row per (thread_id, checkpoint_ns, checkpoint_id) — latest state snapshot after each node |
| `checkpoint_blobs` | Serialised state blobs (channel values) referenced by checkpoints |
| `checkpoint_writes` | Pending writes not yet committed to a checkpoint (in-flight nodes) |

Every message exchanged between agents (LLM input, tool call, tool result,
routing decision) is encoded in the state and therefore persisted after each
node boundary.

## How to query agent-to-agent communication

Connect to the PostgreSQL database and run:

```sql
-- List all runs (thread_ids) for a trace
SELECT DISTINCT thread_id, checkpoint_ns, created_at
FROM checkpoints
ORDER BY created_at DESC
LIMIT 20;

-- Inspect the full state at a specific checkpoint
-- (state is stored as JSON bytes; cast or use psql's \x for readability)
SELECT checkpoint_id, parent_checkpoint_id, created_at,
       encode(checkpoint, 'escape') AS state_json
FROM checkpoints
WHERE thread_id = '<your-trace-id>'
ORDER BY created_at ASC;

-- See pending writes (in-flight) for a run
SELECT task_id, channel, encode(value, 'escape') AS value_json
FROM checkpoint_writes
WHERE thread_id = '<your-trace-id>';
```

For a higher-level view, the Langfuse trace (if enabled) shows every agent's
input and output as span-level events without needing to query PostgreSQL directly.

## Consequences

- Two new tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) created
  on first startup.  No manual migration required.
- State size grows proportionally to message count; long supervisor loops with
  many specialist calls will accumulate significant blob data.  A periodic
  `DELETE FROM checkpoints WHERE created_at < NOW() - INTERVAL '30 days'` job
  is recommended for long-running deployments.
- `thread_id` in LangGraph config is the `trace_id` (UUID) set per run.  This
  ties checkpoints to Langfuse traces when both are enabled.
- `MemorySaver` fallback ensures local dev without a database still works.
