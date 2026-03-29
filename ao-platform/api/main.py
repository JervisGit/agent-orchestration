"""AO Platform API — FastAPI application."""

from fastapi import FastAPI

app = FastAPI(title="Agent Orchestration Platform", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok"}
