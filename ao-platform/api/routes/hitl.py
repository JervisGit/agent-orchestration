"""HITL approval endpoints — PostgreSQL-backed with in-memory fallback."""

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.db import get_db

router = APIRouter()

# In-memory fallback
_pending: dict[str, dict] = {}


class ApprovalResolve(BaseModel):
    approved: bool
    reviewer: str = ""
    note: str = ""


class HITLCreateRequest(BaseModel):
    """Generic HITL request body — sent by AppRuntime.maybe_persist_hitl()."""
    request_id: str = ""          # if empty, a UUID is generated server-side
    workflow_id: str = ""
    step_name: str = ""
    payload: dict[str, Any] = {}  # app-specific data: sender, subject, draft_output, etc.


@router.post("/requests")
async def create_hitl_request(body: HITLCreateRequest, conn=Depends(get_db)):
    """Create a new HITL approval request.

    Called by ``AppRuntime.maybe_persist_hitl()`` in any AO-integrated app.
    The ``payload`` is stored as JSONB and is app-specific — it can carry
    email details, agenda summaries, taxpayer records, or anything else the
    reviewing human needs to make a decision.
    """
    request_id = body.request_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    if conn is None:
        _pending[request_id] = {
            "request_id": request_id,
            "workflow_id": body.workflow_id,
            "step_name": body.step_name,
            "status": "pending",
            "payload": body.payload,
            "created_at": now,
        }
        return {"request_id": request_id, "status": "pending"}

    await conn.execute(
        "INSERT INTO ao_hitl_requests"
        " (request_id, workflow_id, step_name, status, payload)"
        " VALUES (%s, %s, %s, 'pending', %s::jsonb)"
        " ON CONFLICT (request_id) DO NOTHING",
        (request_id, body.workflow_id, body.step_name, __import__("json").dumps(body.payload)),
    )
    return {"request_id": request_id, "status": "pending"}


@router.get("/pending")
async def list_pending(conn=Depends(get_db)):
    if conn is None:
        return {"pending": [v for v in _pending.values() if v["status"] == "pending"]}
    cur = await conn.execute(
        "SELECT * FROM ao_hitl_requests WHERE status = 'pending' ORDER BY created_at DESC"
    )
    return {"pending": await cur.fetchall()}


@router.get("/{request_id}")
async def get_request(request_id: str, conn=Depends(get_db)):
    if conn is None:
        req = _pending.get(request_id)
        if not req:
            raise HTTPException(status_code=404, detail="Request not found")
        return req
    cur = await conn.execute(
        "SELECT * FROM ao_hitl_requests WHERE request_id = %s", (request_id,)
    )
    req = await cur.fetchone()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    return req


@router.post("/{request_id}/resolve")
async def resolve_request(request_id: str, body: ApprovalResolve, conn=Depends(get_db)):
    """Mark a HITL request as approved or rejected in the platform DB.

    Note: this endpoint only updates the status in ao_hitl_requests.
    The actual app-specific action (e.g. updating a taxpayer record) is
    executed by calling the action_webhook stored in the request payload,
    which the dashboard does separately on approval.
    """
    status = "approved" if body.approved else "rejected"
    if conn is None:
        req = _pending.get(request_id)
        if not req:
            raise HTTPException(status_code=404, detail="Request not found")
        if req["status"] != "pending":
            raise HTTPException(status_code=400, detail="Request already resolved")
        req.update(status=status, reviewer=body.reviewer, note=body.note)
        return {"status": status, "request_id": request_id}
    cur = await conn.execute(
        "UPDATE ao_hitl_requests SET status=%s, reviewer=%s, note=%s, resolved_at=%s"
        " WHERE request_id=%s AND status='pending' RETURNING request_id",
        (status, body.reviewer, body.note, datetime.now(timezone.utc), request_id),
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Request not found or already resolved")
    return {"status": status, "request_id": request_id}
