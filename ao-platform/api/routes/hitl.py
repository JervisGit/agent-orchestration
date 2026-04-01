"""HITL approval endpoints — PostgreSQL-backed with in-memory fallback."""

from datetime import datetime, timezone

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
