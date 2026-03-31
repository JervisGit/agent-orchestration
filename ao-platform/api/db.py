"""Shared PostgreSQL helper for the AO Platform API.

Uses psycopg v3 async. Works identically with:
  - local Docker:  DATABASE_URL=postgresql://ao:localdev@localhost:5432/ao
  - Azure Flexible Server: set DATABASE_URL in the environment / Key Vault
"""

import os
from typing import AsyncGenerator

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://ao:localdev@localhost:5432/ao")


async def get_db() -> AsyncGenerator:
    """FastAPI Depends-compatible: yields one psycopg AsyncConnection per request."""
    try:
        async with await psycopg.AsyncConnection.connect(
            DATABASE_URL, row_factory=dict_row
        ) as conn:
            yield conn
    except Exception:
        yield None  # graceful degradation — routes fall back to in-memory
