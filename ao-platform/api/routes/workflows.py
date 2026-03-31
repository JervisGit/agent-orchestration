"""Workflow management endpoints — PostgreSQL-backed with in-memory fallback."""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.db import get_db

router = APIRouter()

# In-memory fallback (used when DB is unavailable)
_workflows: dict[str, dict] = {}
_runs: dict[str, dict] = {}


class WorkflowCreate(BaseModel):
    workflow_id: str
    app_id: str
    pattern: str = "linear"  # linear, router, supervisor, planner
    description: str = ""


class WorkflowRunRequest(BaseModel):
    input_data: dict = {}
    hitl_enabled: bool = False


@router.get("/")
async def list_workflows(conn=Depends(get_db)):
    if conn is None:
        return {"workflows": list(_workflows.values())}
    cur = await conn.execute("SELECT * FROM ao_workflows ORDER BY created_at DESC")
    return {"workflows": await cur.fetchall()}


@router.post("/")
async def create_workflow(body: WorkflowCreate, conn=Depends(get_db)):
    if conn is None:
        _workflows[body.workflow_id] = body.model_dump()
        return {"status": "created", "workflow_id": body.workflow_id}
    await conn.execute(
        "INSERT INTO ao_workflows (workflow_id, app_id, pattern, description)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (workflow_id) DO UPDATE"
        " SET app_id=EXCLUDED.app_id, pattern=EXCLUDED.pattern, description=EXCLUDED.description",
        (body.workflow_id, body.app_id, body.pattern, body.description),
    )
    return {"status": "created", "workflow_id": body.workflow_id}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, conn=Depends(get_db)):
    if conn is None:
        run = _runs.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return run
    cur = await conn.execute("SELECT * FROM ao_workflow_runs WHERE run_id = %s", (run_id,))
    run = await cur.fetchone()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/{workflow_id}")
async def get_workflow(workflow_id: str, conn=Depends(get_db)):
    if conn is None:
        wf = _workflows.get(workflow_id)
        if not wf:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return wf
    cur = await conn.execute(
        "SELECT * FROM ao_workflows WHERE workflow_id = %s", (workflow_id,)
    )
    wf = await cur.fetchone()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return wf


@router.post("/{workflow_id}/run")
async def run_workflow(workflow_id: str, body: WorkflowRunRequest, conn=Depends(get_db)):
    run_id = str(uuid.uuid4())
    if conn is None:
        if workflow_id not in _workflows:
            raise HTTPException(status_code=404, detail="Workflow not found")
        _runs[run_id] = {
            "run_id": run_id, "workflow_id": workflow_id,
            "status": "queued", "input_data": body.input_data,
        }
        return {"run_id": run_id, "status": "queued"}
    cur = await conn.execute(
        "SELECT 1 FROM ao_workflows WHERE workflow_id = %s", (workflow_id,)
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Workflow not found")
    await conn.execute(
        "INSERT INTO ao_workflow_runs (run_id, workflow_id, input_data)"
        " VALUES (%s, %s, %s::jsonb)",
        (run_id, workflow_id, json.dumps(body.input_data)),
    )
    return {"run_id": run_id, "status": "queued"}


@router.get("/{workflow_id}/runs")
async def list_runs(workflow_id: str, conn=Depends(get_db)):
    if conn is None:
        return {"runs": [r for r in _runs.values() if r["workflow_id"] == workflow_id]}
    cur = await conn.execute(
        "SELECT * FROM ao_workflow_runs WHERE workflow_id = %s ORDER BY created_at DESC",
        (workflow_id,),
    )
    return {"runs": await cur.fetchall()}



class WorkflowCreate(BaseModel):
    workflow_id: str
    app_id: str
    pattern: str = "linear"  # linear, router, supervisor, planner
    description: str = ""


class WorkflowRunRequest(BaseModel):
    input_data: dict = {}
    hitl_enabled: bool = False


@router.get("/")
async def list_workflows():
    """List all registered workflows."""
    return {"workflows": list(_workflows.values())}


@router.post("/")
async def create_workflow(body: WorkflowCreate):
    """Register a new workflow."""
    _workflows[body.workflow_id] = body.model_dump()
    return {"status": "created", "workflow_id": body.workflow_id}


@router.get("/{workflow_id}")
async def get_workflow(workflow_id: str):
    """Get workflow details."""
    wf = _workflows.get(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return wf


@router.post("/{workflow_id}/run")
async def run_workflow(workflow_id: str, body: WorkflowRunRequest):
    """Trigger a workflow run."""
    if workflow_id not in _workflows:
        raise HTTPException(status_code=404, detail="Workflow not found")

    import uuid

    run_id = str(uuid.uuid4())
    _runs[run_id] = {
        "run_id": run_id,
        "workflow_id": workflow_id,
        "status": "queued",
        "input_data": body.input_data,
        "hitl_enabled": body.hitl_enabled,
    }
    # TODO: dispatch to engine asynchronously
    return {"run_id": run_id, "status": "queued"}


@router.get("/{workflow_id}/runs")
async def list_runs(workflow_id: str):
    """List runs for a workflow."""
    runs = [r for r in _runs.values() if r["workflow_id"] == workflow_id]
    return {"runs": runs}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    """Get run details."""
    run = _runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run
