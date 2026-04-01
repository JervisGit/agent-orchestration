"""Tool registry API — CRUD for ao_tools."""

import logging
import os
from typing import Any

import psycopg
from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

_DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://ao:localdev@localhost:5432/ao")

# In-memory fallback used when Postgres is unavailable
_tools_mem: list[dict] = []


class ToolCreate(BaseModel):
    app_id: str
    name: str
    type: str = "custom"
    description: str = ""
    endpoint: str | None = None
    connection_secret: str | None = None
    params: dict[str, Any] = {}


@router.get("/")
async def list_tools(app_id: str | None = None):
    """Return all registered tools, optionally filtered by app_id."""
    try:
        async with await psycopg.AsyncConnection.connect(_DATABASE_URL) as conn:
            if app_id:
                cur = await conn.execute(
                    "SELECT id, app_id, name, type, description, endpoint, params, created_at"
                    " FROM ao_tools WHERE app_id = %s ORDER BY name",
                    (app_id,),
                )
            else:
                cur = await conn.execute(
                    "SELECT id, app_id, name, type, description, endpoint, params, created_at"
                    " FROM ao_tools ORDER BY app_id, name"
                )
            cols = [d.name for d in cur.description]
            rows = await cur.fetchall()
            return {"tools": [dict(zip(cols, r)) for r in rows]}
    except Exception as exc:
        logger.warning("DB unavailable, using in-memory store: %s", exc)
        tools = [t for t in _tools_mem if not app_id or t.get("app_id") == app_id]
        return {"tools": tools}


@router.post("/")
async def create_tool(body: ToolCreate):
    """Register or upsert a tool definition."""
    tool = body.model_dump()
    try:
        async with await psycopg.AsyncConnection.connect(_DATABASE_URL) as conn:
            await conn.execute(
                """
                INSERT INTO ao_tools (app_id, name, type, description, endpoint, connection_secret, params)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (app_id, name) DO UPDATE
                    SET type              = EXCLUDED.type,
                        description       = EXCLUDED.description,
                        endpoint          = EXCLUDED.endpoint,
                        connection_secret = EXCLUDED.connection_secret,
                        params            = EXCLUDED.params
                """,
                (
                    tool["app_id"],
                    tool["name"],
                    tool["type"],
                    tool["description"],
                    tool["endpoint"],
                    tool["connection_secret"],
                    psycopg.types.json.Jsonb(tool["params"]),
                ),
            )
            await conn.commit()
    except Exception as exc:
        logger.warning("DB write failed, storing in-memory: %s", exc)
        _tools_mem.append(tool)
    return tool


@router.delete("/{app_id}/{name}")
async def delete_tool(app_id: str, name: str):
    """Remove a tool from the registry."""
    try:
        async with await psycopg.AsyncConnection.connect(_DATABASE_URL) as conn:
            await conn.execute(
                "DELETE FROM ao_tools WHERE app_id = %s AND name = %s",
                (app_id, name),
            )
            await conn.commit()
    except Exception as exc:
        logger.warning("DB delete failed: %s", exc)
        _tools_mem[:] = [t for t in _tools_mem if not (t["app_id"] == app_id and t["name"] == name)]
    return {"deleted": True}
