"""DSAI Apps API — register manifests and list registered apps/agents."""

import logging
import os

import psycopg
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

_DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://ao:localdev@localhost:5432/ao")

# In-memory fallback used when Postgres is unavailable
_apps_mem: list[dict] = []


class AppCreate(BaseModel):
    app_id: str
    display_name: str
    description: str = ""
    pattern: str = "router"
    manifest_yaml: str | None = None


class ManifestUpload(BaseModel):
    manifest_yaml: str


@router.get("/")
async def list_apps():
    """Return all registered apps with their agent count."""
    try:
        async with await psycopg.AsyncConnection.connect(_DATABASE_URL) as conn:
            cur = await conn.execute(
                "SELECT app_id, display_name, description, pattern, created_at"
                " FROM ao_apps ORDER BY display_name"
            )
            cols = [d.name for d in cur.description]
            apps = [dict(zip(cols, r)) for r in await cur.fetchall()]
            for app in apps:
                cnt_cur = await conn.execute(
                    "SELECT COUNT(*) FROM ao_app_agents WHERE app_id = %s",
                    (app["app_id"],),
                )
                app["agent_count"] = (await cnt_cur.fetchone())[0]
            return {"apps": apps}
    except Exception as exc:
        logger.warning("DB unavailable, using in-memory store: %s", exc)
        return {"apps": _apps_mem}


@router.get("/{app_id}")
async def get_app(app_id: str):
    """Return app detail including all registered agents and their tool lists."""
    try:
        async with await psycopg.AsyncConnection.connect(_DATABASE_URL) as conn:
            cur = await conn.execute(
                "SELECT * FROM ao_apps WHERE app_id = %s", (app_id,)
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="App not found")
            cols = [d.name for d in cur.description]
            app = dict(zip(cols, row))

            a_cur = await conn.execute(
                "SELECT agent_name, model, tool_names, hitl_condition"
                " FROM ao_app_agents WHERE app_id = %s ORDER BY agent_name",
                (app_id,),
            )
            a_cols = [d.name for d in a_cur.description]
            app["agents"] = [dict(zip(a_cols, r)) for r in await a_cur.fetchall()]

            t_cur = await conn.execute(
                "SELECT name, type, description, endpoint FROM ao_tools"
                " WHERE app_id = %s ORDER BY name",
                (app_id,),
            )
            t_cols = [d.name for d in t_cur.description]
            app["tools"] = [dict(zip(t_cols, r)) for r in await t_cur.fetchall()]

            return app
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("DB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable")


@router.post("/")
async def create_app(body: AppCreate):
    """Register or update an app."""
    app = body.model_dump()
    try:
        async with await psycopg.AsyncConnection.connect(_DATABASE_URL) as conn:
            await conn.execute(
                """
                INSERT INTO ao_apps (app_id, display_name, description, pattern, manifest_yaml)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (app_id) DO UPDATE
                    SET display_name  = EXCLUDED.display_name,
                        description   = EXCLUDED.description,
                        pattern       = EXCLUDED.pattern,
                        manifest_yaml = EXCLUDED.manifest_yaml,
                        updated_at    = NOW()
                """,
                (app["app_id"], app["display_name"], app["description"],
                 app["pattern"], app.get("manifest_yaml")),
            )
            await conn.commit()
    except Exception as exc:
        logger.warning("DB write failed, storing in-memory: %s", exc)
        _apps_mem.append(app)
    return app


@router.post("/{app_id}/manifest")
async def upload_manifest(app_id: str, body: ManifestUpload):
    """Parse a YAML manifest and sync app, agents, and tools to the platform DB."""
    try:
        data = yaml.safe_load(body.manifest_yaml)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid YAML: {exc}")

    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Manifest must be a YAML mapping")

    app_cfg   = data.get("app", {}) or {}
    agents    = data.get("agents", []) or []
    tools_cfg = data.get("tools", [])  or []

    display_name = app_cfg.get("name", app_id)
    description  = app_cfg.get("description", "")
    pattern      = app_cfg.get("pattern", "router")

    try:
        async with await psycopg.AsyncConnection.connect(_DATABASE_URL) as conn:
            # Upsert app row
            await conn.execute(
                """
                INSERT INTO ao_apps (app_id, display_name, description, pattern, manifest_yaml)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (app_id) DO UPDATE
                    SET display_name  = EXCLUDED.display_name,
                        description   = EXCLUDED.description,
                        pattern       = EXCLUDED.pattern,
                        manifest_yaml = EXCLUDED.manifest_yaml,
                        updated_at    = NOW()
                """,
                (app_id, display_name, description, pattern, body.manifest_yaml),
            )

            # Sync agents
            for agent in agents:
                aname = agent.get("name", "")
                if not aname:
                    continue
                await conn.execute(
                    """
                    INSERT INTO ao_app_agents (app_id, agent_name, model, tool_names, hitl_condition)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (app_id, agent_name) DO UPDATE
                        SET model          = EXCLUDED.model,
                            tool_names     = EXCLUDED.tool_names,
                            hitl_condition = EXCLUDED.hitl_condition
                    """,
                    (app_id, aname, agent.get("model", ""),
                     agent.get("tools", []), agent.get("hitl_condition")),
                )

            # Sync tools
            for tool in tools_cfg:
                tname = tool.get("name", "")
                if not tname:
                    continue
                await conn.execute(
                    """
                    INSERT INTO ao_tools (app_id, name, type, description, endpoint)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (app_id, name) DO UPDATE
                        SET type        = EXCLUDED.type,
                            description = EXCLUDED.description,
                            endpoint    = EXCLUDED.endpoint
                    """,
                    (app_id, tname, tool.get("type", "custom"),
                     tool.get("description", ""), tool.get("endpoint")),
                )

            await conn.commit()
    except Exception as exc:
        logger.warning("DB sync failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")

    return {
        "app_id":        app_id,
        "synced_agents": len(agents),
        "synced_tools":  len(tools_cfg),
    }
