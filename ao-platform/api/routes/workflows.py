"""Workflow management endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

# In-memory store for demo. Production: PostgreSQL.
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
