# ADR-018: Per-User Long-Term Memory for Agent Applications

## Status
Accepted

## Context

### What exists today

The platform has two memory tiers already implemented:

| Tier | Implementation | Lifetime | Keyed by |
|---|---|---|---|
| **Short-term** | `ao.memory.short_term.ShortTermMemory` — Redis | TTL (default 1 h) | `session_id` |
| **Long-term (document RAG)** | `ao.memory.long_term.LongTermMemory` — PostgreSQL + pgvector | Permanent | `(app_id, namespace, key)` |

`LongTermMemory` (table `ao_long_term_memory`) is wired **only** in the RAG search example to store and retrieve ingested documents.  It has no `user_id` column and therefore cannot distinguish records belonging to different users.

Neither the graph compliance app nor the RAG search app stores **per-user** memory — facts, preferences, past investigation summaries, or prior search context — that persists across sessions.

### Problem

An officer who runs ten compliance investigations on the same entity today starts each session with zero context of what she already found.  A citizen who previously searched for a specific regulation and annotated it gets no personalised retrieval the next time.  Agents cannot learn from a user's history within the same application.

### Scope

This ADR covers **per-user** long-term memory for the existing examples (graph_compliance, rag_search) and how the platform should support it going forward.  It does **not** cover cross-application memory, agent-to-agent observability (ADR-019), or HITL audit trails.

---

## Options Considered

### Option A — Extend `ao_long_term_memory` with `user_id`

Add a `user_id TEXT` column to `ao_long_term_memory` and a composite unique index on `(app_id, namespace, user_id, key)`.  Store user facts/summaries under `namespace="user_memory"`.  Use existing pgvector for semantic recall.

**Pros:**
- Reuses the existing PostgreSQL + pgvector stack (ADR-003).
- Already queryable: both exact key lookup and vector similarity across a user's records.
- Single migration (`ALTER TABLE ... ADD COLUMN user_id TEXT`).
- Audit trail falls out naturally from PostgreSQL row history / `updated_at`.
- pgvector cosine search ranks *semantically relevant* past memories on recall.

**Cons:**
- The current ON CONFLICT key is `(app_id, namespace, key)` — adding `user_id` changes the constraint; past records need a backfill `user_id = 'system'`.
- One table holds both document RAG chunks and user memories — different access patterns, may need separate indices.

---

### Option B — Markdown files in Azure Blob Storage

Store one `.md` file per user (`{container}/{app_id}/{user_id}/memory.md`).  Agents append sections.  Azure Blob Storage versioning provides an audit trail.

**Pros:**
- Zero schema migrations.
- Human-readable — officer can inspect or export their memory file.
- Blob Storage versioning is a cheap audit trail.
- EasyAuth + Managed Identity controls access at the Blob level.

**Cons:**
- No vector search — retrieval is whole-file read only; impractical once the file grows.
- Concurrent appends need locking (Blob leases) or a serialised write queue.
- Full-file read + regex extraction for recall doesn't scale.
- Hard to enforce append-only at the API level — any writer can overwrite.

---

### Option C — Separate `ao_user_memory` table (append-only log)

A dedicated table: `(id, app_id, user_id, agent_name, memory_type, content TEXT, embedding vector, created_at)`.  **Never update or delete rows** — only INSERT.  Agents recall via `SELECT … ORDER BY embedding <=> $1 LIMIT k`.  Summaries and corrections are additional rows with `memory_type='correction'`.

**Pros:**
- Immutable / append-only is the strongest auditability guarantee.
- Simple schema — no `ON CONFLICT` complexity.
- Easy to project backwards through time for a user (`ORDER BY created_at DESC`).
- Separate table = separate vacuum/index tuning.

**Cons:**
- Table grows unboundedly; periodic compaction/summarisation needed (an agent "memory consolidator" job).
- No keyed overwrite — a tool cannot update a known preference; it must append a correction row and recall logic must pick the most recent.
- Recall requires a vector search every time, even for structured facts.

---

### Option D — Azure AI Search (hybrid search)

Push memories into an Azure AI Search index with a `user_id` filter.  Use hybrid BM25 + vector for recall.

**Pros:**
- Best recall quality: hybrid search outperforms pure vector search for short factual queries.
- Managed service — no index maintenance.

**Cons:**
- New service dependency not in the current stack (ADR-003).
- Cost: per-document ingestion + query fees.
- Ingestion is async (index lag).
- Overkill for per-user notes at current scale.

---

## Decision

**Option C — dedicated `ao_user_memory` append-only table**, with **Option A's `user_id` extension** applied to `ao_long_term_memory` as a parallel change to tag existing document RAG records.

Rationale:

1. **Auditability**: An append-only log gives a complete audit trail of what agents wrote about a user with no possibility of silent overwrites.  Compliance applications require this.
2. **PostgreSQL reuse**: No new services.  pgvector cosine search works well at the scale of per-user memories (typically < 10,000 rows per user).
3. **Separation of concerns**: Document RAG (`ao_long_term_memory`) and user memories (`ao_user_memory`) have different access patterns, TTL policies, and index strategies.
4. **Immutability by default, not by policy**: The table schema enforces append-only because there is no `UPDATE` path in the `UserMemory` interface.  Agents that need to correct a fact append a `correction` row; the recall query includes a deduplication window.

### Mutability policy

| Memory type | Write rule | Agent access |
|---|---|---|
| `observation` | Append only | Specialist agents (tools) |
| `preference` | Append-only new value; recall picks latest per `memory_key` | Specialist agents |
| `summary` | Append only; old summaries retained | Memory consolidator job only |
| `correction` | Append only | Specialist agents |

Agents may **read** their own app's user memories.  They **may not** read another app's user memories.  A nightly consolidator (or on-demand summariser) compresses old observations into summary rows to bound recall latency.

---

## Schema

```sql
CREATE TABLE ao_user_memory (
    id           BIGSERIAL PRIMARY KEY,
    app_id       TEXT        NOT NULL,
    user_id      TEXT        NOT NULL,           -- sub claim from OIDC token
    agent_name   TEXT        NOT NULL DEFAULT '', -- manifest agents[].name
    memory_type  TEXT        NOT NULL DEFAULT 'observation',  -- observation|preference|summary|correction
    memory_key   TEXT        NOT NULL DEFAULT '', -- structured key for preference recall ('' for free-form)
    content      TEXT        NOT NULL,
    embedding    vector(1536),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    -- No updated_at — rows are never updated
);

CREATE INDEX idx_uom_app_user      ON ao_user_memory(app_id, user_id);
CREATE INDEX idx_uom_app_user_key  ON ao_user_memory(app_id, user_id, memory_key)
    WHERE memory_key <> '';
CREATE INDEX idx_uom_embedding     ON ao_user_memory
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

And the migration to `ao_long_term_memory`:

```sql
ALTER TABLE ao_long_term_memory ADD COLUMN user_id TEXT NOT NULL DEFAULT 'system';
DROP INDEX IF EXISTS ao_long_term_memory_app_id_namespace_key_key;
ALTER TABLE ao_long_term_memory ADD CONSTRAINT uq_ltm_user
    UNIQUE (app_id, namespace, user_id, key);
```

---

## Python interface

```python
class UserMemory:
    """Append-only per-user long-term memory backed by ao_user_memory."""

    async def remember(
        self,
        user_id: str,
        content: str,
        agent_name: str = "",
        memory_type: str = "observation",
        memory_key: str = "",
        embedding: list[float] | None = None,
    ) -> None: ...

    async def recall(
        self,
        user_id: str,
        query_embedding: list[float],
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[dict]: ...

    async def recall_preference(
        self,
        user_id: str,
        memory_key: str,
    ) -> str | None:
        """Return the most recently appended value for a structured preference key."""
        ...
```

Tools that want to store user facts call `remember()`; tools that want to surface past context call `recall()`.  Both are opt-in — existing tools remain unchanged.

---

## Where `user_id` comes from

The `user_id` for memory rows is the `sub` (subject) claim from the OIDC token, extracted from the `IdentityContext.claims` dict.  This requires the caller to pass `_identity` in graph state (see ADR-013 and Q2 above — EasyAuth provides this for container apps once enabled).

For service-mode agents with no user context, `user_id = 'system'` is used and memory is effectively app-scoped.

---

## What graph_compliance and rag_search should store

| App | Memory type | Example content |
|---|---|---|
| graph_compliance | `observation` | "Apex Holdings Pte Ltd — flagged circular ownership, confirmed 2026-04-05" |
| graph_compliance | `preference` | `memory_key="preferred_investigation_depth"` → `"3_hops"` |
| rag_search | `observation` | "User previously found Regulation 14C relevant for pension fund questions" |
| rag_search | `preference` | `memory_key="preferred_citation_style"` → `"footnote"` |

---

## Consequences

- New `ao_user_memory` table; migration script required before deployment.
- `ao.memory.user_memory.UserMemory` class to be added to `ao-core`.
- EasyAuth must be enabled on graph_compliance and rag_search container apps for `user_id` to be populated from real token claims.
- A consolidator job (worker or Azure Function) is needed to prevent unbounded table growth.  This is out of scope for the initial implementation — a row-count alert at 100k rows per user is sufficient as an interim control.
- Document RAG records in `ao_long_term_memory` get `user_id = 'system'` on migration — no functional change.
