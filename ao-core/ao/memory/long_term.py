"""Long-term memory — PostgreSQL + pgvector persistent memory.

Stores persistent facts, user preferences, and embeddings that
survive across sessions. Supports vector similarity search.

Implementation note: all DB operations use the synchronous psycopg API
wrapped in ``asyncio.to_thread()``, which makes them compatible with any
event-loop implementation (including Windows' ProactorEventLoop).
"""

import asyncio
import json
import logging
from typing import Any

import psycopg

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ao_long_term_memory (
    id SERIAL PRIMARY KEY,
    app_id TEXT NOT NULL,
    namespace TEXT NOT NULL DEFAULT 'default',
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(app_id, namespace, key)
);
CREATE INDEX IF NOT EXISTS idx_ltm_app_ns ON ao_long_term_memory(app_id, namespace);
"""


class LongTermMemory:
    """PostgreSQL + pgvector backed long-term memory."""

    def __init__(self, connection_string: str, app_id: str):
        self._conn_str = connection_string
        self._app_id = app_id

    # ── Synchronous implementations (run in thread pool) ─────────────

    def _initialize(self) -> None:
        with psycopg.connect(self._conn_str) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(CREATE_TABLE_SQL)
            conn.commit()
        logger.info("Long-term memory tables initialized for app=%s", self._app_id)

    def _store(
        self,
        key: str,
        value: Any,
        namespace: str,
        embedding: list[float] | None,
    ) -> None:
        with psycopg.connect(self._conn_str) as conn:
            if embedding:
                conn.execute(
                    """
                    INSERT INTO ao_long_term_memory (app_id, namespace, key, value, embedding, updated_at)
                    VALUES (%s, %s, %s, %s::jsonb, %s::vector, NOW())
                    ON CONFLICT (app_id, namespace, key)
                    DO UPDATE SET value = EXCLUDED.value, embedding = EXCLUDED.embedding, updated_at = NOW()
                    """,
                    (self._app_id, namespace, key, json.dumps(value), str(embedding)),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO ao_long_term_memory (app_id, namespace, key, value, updated_at)
                    VALUES (%s, %s, %s, %s::jsonb, NOW())
                    ON CONFLICT (app_id, namespace, key)
                    DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                    """,
                    (self._app_id, namespace, key, json.dumps(value)),
                )
            conn.commit()

    def _retrieve(self, key: str, namespace: str) -> Any | None:
        with psycopg.connect(self._conn_str) as conn:
            cur = conn.execute(
                "SELECT value FROM ao_long_term_memory WHERE app_id = %s AND namespace = %s AND key = %s",
                (self._app_id, namespace, key),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def _search_similar(
        self,
        query_embedding: list[float],
        namespace: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        with psycopg.connect(self._conn_str) as conn:
            cur = conn.execute(
                """
                SELECT key, value, 1 - (embedding <=> %s::vector) AS similarity
                FROM ao_long_term_memory
                WHERE app_id = %s AND namespace = %s AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (str(query_embedding), self._app_id, namespace, str(query_embedding), top_k),
            )
            rows = cur.fetchall()
            return [{"key": r[0], "value": r[1], "similarity": r[2]} for r in rows]

    def _delete(self, key: str, namespace: str) -> None:
        with psycopg.connect(self._conn_str) as conn:
            conn.execute(
                "DELETE FROM ao_long_term_memory WHERE app_id = %s AND namespace = %s AND key = %s",
                (self._app_id, namespace, key),
            )
            conn.commit()

    # ── Async public API ──────────────────────────────────────────────

    async def initialize(self) -> None:
        """Create tables if they don't exist."""
        await asyncio.to_thread(self._initialize)

    async def store(
        self,
        key: str,
        value: Any,
        namespace: str = "default",
        embedding: list[float] | None = None,
    ) -> None:
        """Store or update a memory entry."""
        await asyncio.to_thread(self._store, key, value, namespace, embedding)

    async def retrieve(self, key: str, namespace: str = "default") -> Any | None:
        """Retrieve a memory entry by key."""
        return await asyncio.to_thread(self._retrieve, key, namespace)

    async def search_similar(
        self,
        query_embedding: list[float],
        namespace: str = "default",
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Find entries most similar to the query embedding via cosine distance."""
        return await asyncio.to_thread(self._search_similar, query_embedding, namespace, top_k)

    async def delete(self, key: str, namespace: str = "default") -> None:
        """Delete a memory entry."""
        await asyncio.to_thread(self._delete, key, namespace)

