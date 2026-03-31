"""Policy management endpoints — PostgreSQL-backed with in-memory fallback."""

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.db import get_db

router = APIRouter()

# In-memory fallback
_policies: dict[str, dict] = {}


class PolicyCreate(BaseModel):
    app_id: str
    name: str
    stage: str   # pre_execution, post_execution, runtime
    action: str = "block"  # block, redact, warn, log
    provider: str | None = None
    params: dict = {}


@router.get("/")
async def list_policies(app_id: str | None = None, conn=Depends(get_db)):
    if conn is None:
        pols = list(_policies.values())
        if app_id:
            pols = [p for p in pols if p["app_id"] == app_id]
        return {"policies": pols}
    if app_id:
        cur = await conn.execute(
            "SELECT * FROM ao_policies WHERE app_id = %s ORDER BY created_at", (app_id,)
        )
    else:
        cur = await conn.execute("SELECT * FROM ao_policies ORDER BY app_id, name")
    return {"policies": await cur.fetchall()}


@router.post("/")
async def create_policy(body: PolicyCreate, conn=Depends(get_db)):
    if conn is None:
        key = f"{body.app_id}:{body.name}"
        _policies[key] = body.model_dump()
        return {"status": "created"}
    await conn.execute(
        "INSERT INTO ao_policies (app_id, name, stage, action, params)"
        " VALUES (%s, %s, %s, %s, %s::jsonb)"
        " ON CONFLICT (app_id, name) DO UPDATE"
        " SET stage=EXCLUDED.stage, action=EXCLUDED.action, params=EXCLUDED.params",
        (body.app_id, body.name, body.stage, body.action, json.dumps(body.params)),
    )
    return {"status": "created"}


@router.get("/{app_id}/{name}")
async def get_policy(app_id: str, name: str, conn=Depends(get_db)):
    if conn is None:
        p = _policies.get(f"{app_id}:{name}")
        if not p:
            raise HTTPException(status_code=404, detail="Policy not found")
        return p
    cur = await conn.execute(
        "SELECT * FROM ao_policies WHERE app_id = %s AND name = %s", (app_id, name)
    )
    p = await cur.fetchone()
    if not p:
        raise HTTPException(status_code=404, detail="Policy not found")
    return p


@router.delete("/{app_id}/{name}")
async def delete_policy(app_id: str, name: str, conn=Depends(get_db)):
    if conn is None:
        key = f"{app_id}:{name}"
        if key not in _policies:
            raise HTTPException(status_code=404, detail="Policy not found")
        del _policies[key]
        return {"status": "deleted"}
    cur = await conn.execute(
        "DELETE FROM ao_policies WHERE app_id = %s AND name = %s RETURNING id",
        (app_id, name),
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Policy not found")
    return {"status": "deleted"}



class PolicyCreate(BaseModel):
    app_id: str
    name: str
    stage: str  # pre_execution, post_execution, runtime
    action: str = "block"  # block, redact, warn, log
    provider: str | None = None
    params: dict = {}


@router.get("/")
async def list_policies(app_id: str | None = None):
    """List policies, optionally filtered by app_id."""
    policies = list(_policies.values())
    if app_id:
        policies = [p for p in policies if p["app_id"] == app_id]
    return {"policies": policies}


@router.post("/")
async def create_policy(body: PolicyCreate):
    """Create a new policy."""
    key = f"{body.app_id}:{body.name}"
    _policies[key] = body.model_dump()
    return {"status": "created", "policy_key": key}


@router.get("/{app_id}/{name}")
async def get_policy(app_id: str, name: str):
    """Get a specific policy."""
    key = f"{app_id}:{name}"
    policy = _policies.get(key)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    return policy


@router.delete("/{app_id}/{name}")
async def delete_policy(app_id: str, name: str):
    """Delete a policy."""
    key = f"{app_id}:{name}"
    if key not in _policies:
        raise HTTPException(status_code=404, detail="Policy not found")
    del _policies[key]
    return {"status": "deleted"}
