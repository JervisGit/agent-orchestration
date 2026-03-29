"""HITL approval endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

# In-memory store for demo. Production: backed by HITLManager.
_pending: dict[str, dict] = {}


class ApprovalResolve(BaseModel):
    approved: bool
    reviewer: str = ""
    note: str = ""


@router.get("/pending")
async def list_pending():
    """List all pending approval requests."""
    return {"pending": [v for v in _pending.values() if v["status"] == "pending"]}


@router.get("/{request_id}")
async def get_request(request_id: str):
    """Get approval request details."""
    req = _pending.get(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    return req


@router.post("/{request_id}/resolve")
async def resolve_request(request_id: str, body: ApprovalResolve):
    """Approve or reject a pending request."""
    req = _pending.get(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req["status"] != "pending":
        raise HTTPException(status_code=400, detail="Request already resolved")

    req["status"] = "approved" if body.approved else "rejected"
    req["reviewer"] = body.reviewer
    req["note"] = body.note

    # TODO: signal HITLManager to unblock the workflow
    return {"status": req["status"], "request_id": request_id}
