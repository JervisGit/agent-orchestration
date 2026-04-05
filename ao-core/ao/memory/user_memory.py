"""Per-user long-term memory — append-only PostgreSQL table (ADR-018).

Stores observations, preferences, and summaries for individual users across
sessions.  Rows are NEVER updated or deleted — only INSERTed.  This is an
architectural guarantee: corrections are additional rows, not overwrites.

Table: ao_user_memory
  id          BIGSERIAL PRIMARY KEY
  app_id      TEXT NOT NULL
  user_id     TEXT NOT NULL           -- OIDC sub claim
  agent_name  TEXT NOT NULL DEFAULT ''
  memory_type TEXT NOT NULL DEFAULT 'observation'
              -- 'observation' | 'preference' | 'summary' | 'correction'
  memory_key  TEXT NOT NULL DEFAULT ''  -- structured key for preference lookup
  content     TEXT NOT NULL
  embedding   vector(1536)            -- populated when embed= is provided
  created_at  TIMESTAMPTZ DEFAULT NOW()

Index strategy:
  idx_uom_app_user      ON (app_id, user_id)            -- base filter
  idx_uom_app_user_key  ON (app_id, user_id, memory_key) WHERE memory_key <> ''
  idx_uom_embedding     ivfflat cosine               -- semantic recall
"""

import asyncio
import json
import logging
from typing import Any

import psycopg

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ao_user_memory (
    id          BIGSERIAL PRIMARY KEY,
    app_id      TEXT        NOT NULL,
    user_id     TEXT        NOT NULL,
    agent_name  TEXT        NOT NULL DEFAULT '',
    memory_type TEXT        NOT NULL DEFAULT 'observation',
    memory_key  TEXT        NOT NULL DEFAULT '',
    content     TEXT        NOT NULL,
    embedding   vector(1536),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_uom_app_user
    ON ao_user_memory(app_id, user_id);
CREATE INDEX IF NOT EXISTS idx_uom_app_user_key
    ON ao_user_memory(app_id, user_id, memory_key)
    WHERE memory_key <> '';
"""

# IVFFlat index is created separately — requires rows to exist first.
_CREATE_VECTOR_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_uom_embedding
    ON ao_user_memory
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
"""


class UserMemory:
    """Append-only per-user long-term memory backed by PostgreSQL + pgvector."""

    def __init__(self, connection_string: str, app_id: str) -> None:
        self._conn_str = connection_string
        self._app_id = app_id

    # ── Sync implementations (run in thread pool) ─────────────────────

    def _initialize(self) -> None:
        with psycopg.connect(self._conn_str) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(_CREATE_TABLE_SQL)
            try:
                conn.execute(_CREATE_VECTOR_INDEX_SQL)
            except Exception:
                # IVFFlat needs at least 1 row; silently skip on empty table
                pass
            conn.commit()
        logger.info("UserMemory tables initialised for app=%s", self._app_id)

    def _remember(
        self,
        user_id: str,
        content: str,
        agent_name: str,
        memory_type: str,
        memory_key: str,
        embedding: list[float] | None,
    ) -> None:
        with psycopg.connect(self._conn_str) as conn:
            if embedding:
                conn.execute(
                    """
                    INSERT INTO ao_user_memory
                        (app_id, user_id, agent_name, memory_type, memory_key, content, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::vector)
                    """,
                    (self._app_id, user_id, agent_name, memory_type,
                     memory_key, content, str(embedding)),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO ao_user_memory
                        (app_id, user_id, agent_name, memory_type, memory_key, content)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (self._app_id, user_id, agent_name, memory_type, memory_key, content),
                )
            conn.commit()

    def _recall(
        self,
        user_id: str,
        query_embedding: list[float],
        top_k: int,
        memory_type: str | None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT id, agent_name, memory_type, memory_key, content, created_at,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM ao_user_memory
            WHERE app_id = %s AND user_id = %s AND embedding IS NOT NULL
        """
        params: list[Any] = [str(query_embedding), self._app_id, user_id]
        if memory_type:
            sql += " AND memory_type = %s"
            params.append(memory_type)
        sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params.extend([str(query_embedding), top_k])
        with psycopg.connect(self._conn_str) as conn:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "agent_name": r[1],
                "memory_type": r[2],
                "memory_key": r[3],
                "content": r[4],
                "created_at": r[5].isoformat() if r[5] else None,
                "similarity": float(r[6]) if r[6] is not None else None,
            }
            for r in rows
        ]

    def _recall_preference(
        self,
        user_id: str,
        memory_key: str,
    ) -> str | None:
        with psycopg.connect(self._conn_str) as conn:
            cur = conn.execute(
                """
                SELECT content FROM ao_user_memory
                WHERE app_id = %s AND user_id = %s AND memory_key = %s
                  AND memory_type = 'preference'
                ORDER BY created_at DESC LIMIT 1
                """,
                (self._app_id, user_id, memory_key),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def _recent(
        self,
        user_id: str,
        limit: int,
        memory_type: str | None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT id, agent_name, memory_type, memory_key, content, created_at
            FROM ao_user_memory
            WHERE app_id = %s AND user_id = %s
        """
        params: list[Any] = [self._app_id, user_id]
        if memory_type:
            sql += " AND memory_type = %s"
            params.append(memory_type)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with psycopg.connect(self._conn_str) as conn:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "agent_name": r[1],
                "memory_type": r[2],
                "memory_key": r[3],
                "content": r[4],
                "created_at": r[5].isoformat() if r[5] else None,
            }
            for r in rows
        ]

    # ── Async public API ──────────────────────────────────────────────

    async def initialize(self) -> None:
        """Create ao_user_memory table and indices if they do not exist."""
        await asyncio.to_thread(self._initialize)

    async def remember(
        self,
        user_id: str,
        content: str,
        agent_name: str = "",
        memory_type: str = "observation",
        memory_key: str = "",
        embedding: list[float] | None = None,
    ) -> None:
        """Append a new memory row.  Never overwrites existing rows.

        Parameters
        ----------
        user_id     : OIDC ``sub`` claim (use ``ao.identity.extract.get_user_id``).
        content     : Plain-text memory content.
        agent_name  : Manifest agent name that produced this memory.
        memory_type : 'observation' | 'preference' | 'summary' | 'correction'.
        memory_key  : Structured key for preference recall (leave '' for free-form).
        embedding   : Optional 1536-dim vector for semantic recall.
        """
        await asyncio.to_thread(
            self._remember, user_id, content, agent_name, memory_type, memory_key, embedding
        )

    async def recall(
        self,
        user_id: str,
        query_embedding: list[float],
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return top-k memories most similar to the query embedding."""
        return await asyncio.to_thread(self._recall, user_id, query_embedding, top_k, memory_type)

    async def recall_preference(
        self,
        user_id: str,
        memory_key: str,
    ) -> str | None:
        """Return the most recently written value for a structured preference key."""
        return await asyncio.to_thread(self._recall_preference, user_id, memory_key)

    async def recent(
        self,
        user_id: str,
        limit: int = 10,
        memory_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the most recently written memory rows for a user (no vector needed)."""
        return await asyncio.to_thread(self._recent, user_id, limit, memory_type)
