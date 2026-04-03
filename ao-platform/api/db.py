"""Shared PostgreSQL helper for the AO Platform API.

Uses synchronous psycopg v3 wrapped in asyncio.to_thread() so that route
handlers keep the same await-able interface but work with any event-loop
implementation, including Windows' ProactorEventLoop.
"""

import asyncio
import os
from typing import AsyncGenerator

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://ao:localdev@localhost:5432/ao")


class _AsyncCursorWrapper:
    """Sync psycopg Cursor with async fetchall() / fetchone()."""

    def __init__(self, cursor: psycopg.Cursor) -> None:
        self._cur = cursor

    async def fetchall(self):
        return await asyncio.to_thread(self._cur.fetchall)

    async def fetchone(self):
        return await asyncio.to_thread(self._cur.fetchone)


class _AsyncConnWrapper:
    """Sync psycopg Connection with async execute() / commit()."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    async def execute(self, query, params=None):
        cur = await asyncio.to_thread(self._conn.execute, query, params or ())
        return _AsyncCursorWrapper(cur)

    async def commit(self):
        await asyncio.to_thread(self._conn.commit)


async def get_db() -> AsyncGenerator:
    """FastAPI Depends-compatible: yields one psycopg connection per request."""
    try:
        conn: psycopg.Connection = await asyncio.to_thread(
            lambda: psycopg.connect(DATABASE_URL, row_factory=dict_row)
        )
    except Exception:
        yield None  # graceful degradation — routes fall back to in-memory
        return
    try:
        yield _AsyncConnWrapper(conn)
        await asyncio.to_thread(conn.commit)
    except Exception:
        await asyncio.to_thread(conn.rollback)
        raise
    finally:
        await asyncio.to_thread(conn.close)
