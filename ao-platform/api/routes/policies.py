"""Policy management endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

# In-memory store for demo. Production: PostgreSQL.
_policies: dict[str, dict] = {}


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
