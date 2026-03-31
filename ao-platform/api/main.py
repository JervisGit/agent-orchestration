"""AO Platform API — FastAPI application."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.routes import hitl, policies, workflows

app = FastAPI(title="Agent Orchestration Platform", version="0.1.0")

# Register routers
app.include_router(workflows.router, prefix="/api/workflows", tags=["workflows"])
app.include_router(hitl.router, prefix="/api/hitl", tags=["hitl"])
app.include_router(policies.router, prefix="/api/policies", tags=["policies"])

# Dashboard static files
DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def dashboard():
    return FileResponse(DASHBOARD_DIR / "index.html")
