"""AO Platform API — FastAPI application."""

import logging
import os
import sys
from pathlib import Path

import psycopg
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.routes import hitl, policies, workflows

# ── Structured JSON logging ─────────────────────────────────────────
def _configure_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    try:
        from pythonjsonlogger import jsonlogger
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            jsonlogger.JsonFormatter(
                "%(asctime)s %(name)s %(levelname)s %(message)s",
                rename_fields={"asctime": "timestamp", "levelname": "level"},
            )
        )
        logging.basicConfig(level=log_level, handlers=[handler], force=True)
    except ImportError:
        logging.basicConfig(level=log_level, stream=sys.stdout)

_configure_logging()

app = FastAPI(title="Agent Orchestration Platform", version="0.1.0")

# Register routers
app.include_router(workflows.router, prefix="/api/workflows", tags=["workflows"])
app.include_router(hitl.router, prefix="/api/hitl", tags=["hitl"])
app.include_router(policies.router, prefix="/api/policies", tags=["policies"])

# Dashboard static files
DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

_DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://ao:localdev@localhost:5432/ao")


@app.get("/healthz")
async def healthz():
    """Health check used by ACA/AKS liveness and readiness probes."""
    checks: dict[str, str] = {}

    # Database ping
    try:
        async with await psycopg.AsyncConnection.connect(
            _DATABASE_URL, connect_timeout=3
        ) as conn:
            await conn.execute("SELECT 1")
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"error: {exc}"

    status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": status, "checks": checks}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def dashboard():
    return FileResponse(DASHBOARD_DIR / "index.html")
