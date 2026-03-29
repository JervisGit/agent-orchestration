"""AO Platform API — FastAPI application."""

from fastapi import FastAPI

from api.routes import hitl, policies, workflows

app = FastAPI(title="Agent Orchestration Platform", version="0.1.0")

# Register routers
app.include_router(workflows.router, prefix="/api/workflows", tags=["workflows"])
app.include_router(hitl.router, prefix="/api/hitl", tags=["hitl"])
app.include_router(policies.router, prefix="/api/policies", tags=["policies"])


@app.get("/health")
async def health():
    return {"status": "ok"}
