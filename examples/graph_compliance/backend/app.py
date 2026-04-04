"""Graph Compliance — FastAPI backend.

Demonstrates the **supervisor** pattern with user-delegated identity.
An LLM compliance_planner orchestrates a graph_investigator that uses
NetworkX-backed tools to explore entity relationships and surface risk.

In production, replace the NetworkX tool implementations with Neo4j
Cypher queries — the agents and manifest remain unchanged.

Run (from project root):
    uvicorn backend.app:app --reload --port 8003 --app-dir examples/graph_compliance

Query:
    POST /api/investigate  {"query": "Investigate Apex Holdings Pte Ltd"}
    GET  /api/investigate/stream?q=...   (SSE)
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ao.policy.engine import PolicyEngine
from ao.policy.schema import PolicySet, PolicyStage
from ao.runtime import AppRuntime
from backend.compliance_graph import (
    find_entity,
    get_neighbors,
    find_path,
    get_risk_indicators,
    graph_stats,
    ENTITIES,
    EDGES,
)

# ── Config ────────────────────────────────────────────────────────────
try:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=True)
except (IndexError, OSError):
    pass

FRONTEND_DIR  = Path(__file__).parent.parent / "frontend"
MANIFEST_PATH = Path(__file__).parent.parent / "ao-manifest.yaml"
APP_ID        = "graph_compliance"

# ── Structured logging ────────────────────────────────────────────────
def _configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    try:
        from pythonjsonlogger import jsonlogger
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(jsonlogger.JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        ))
        logging.basicConfig(level=level, handlers=[h], force=True)
    except ImportError:
        logging.basicConfig(level=level, stream=sys.stdout)

_configure_logging()
logger = logging.getLogger("graph_compliance")

# ── AppRuntime ────────────────────────────────────────────────────────
_runtime = AppRuntime.from_env(MANIFEST_PATH)
executor = _runtime.executor
llm      = _runtime.llm

# ── Tool schemas ──────────────────────────────────────────────────────

_FIND_ENTITY_SCHEMA: dict = {
    "name": "find_entity",
    "description": "Look up an entity (company or person) in the compliance graph by name. Returns entity ID, type, risk level, and flags.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Entity name or partial name to search for"},
        },
        "required": ["name"],
    },
}

_GET_NEIGHBORS_SCHEMA: dict = {
    "name": "get_neighbors",
    "description": "Get all direct relationships for an entity in the compliance graph. Optionally filter by relationship type (OWNS, CONTROLS, DIRECTOR_OF, HAS_ACCOUNT, TRANSACTED_WITH, SHARED_DIRECTOR_WITH).",
    "parameters": {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": "Entity ID (e.g. C001, P004, A001)"},
            "relationship_type": {"type": "string", "description": "Optional: filter by relationship type", "default": ""},
        },
        "required": ["entity_id"],
    },
}

_FIND_PATH_SCHEMA: dict = {
    "name": "find_path",
    "description": "Find the shortest connection path between two entities in the compliance graph. Useful for identifying indirect relationships.",
    "parameters": {
        "type": "object",
        "properties": {
            "from_entity_id": {"type": "string", "description": "Starting entity ID"},
            "to_entity_id":   {"type": "string", "description": "Target entity ID"},
        },
        "required": ["from_entity_id", "to_entity_id"],
    },
}

_GET_RISK_SCHEMA: dict = {
    "name": "get_risk_indicators",
    "description": "Get all risk flags, second-degree exposure, and circular ownership indicators for an entity. Returns a composite risk score.",
    "parameters": {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": "Entity ID to assess"},
        },
        "required": ["entity_id"],
    },
}

# ── Register tools (NetworkX-backed) ──────────────────────────────────
# ADR-015: In production, these callables would call Neo4j Cypher queries.
# The agents are completely decoupled — only these four lines change.
executor.register_tool("find_entity",       find_entity,       _FIND_ENTITY_SCHEMA)
executor.register_tool("get_neighbors",     get_neighbors,     _GET_NEIGHBORS_SCHEMA)
executor.register_tool("find_path",         find_path,         _FIND_PATH_SCHEMA)
executor.register_tool("get_risk_indicators", get_risk_indicators, _GET_RISK_SCHEMA)

compiled_graph = executor.compile(state_schema=dict)

# ── Policy engine ─────────────────────────────────────────────────────
policy_engine = PolicyEngine()
policy_engine.register_builtin_rules()

_manifest_policies = _runtime.manifest.policies_inline
_POLICY_FALLBACK = [
    {"name": "content_safety", "stage": "pre_execution",  "action": "block"},
    {"name": "pii_filter",     "stage": "post_execution", "action": "redact"},
]
policies: PolicySet = (
    PolicySet.from_manifest_inline(_manifest_policies)
    if _manifest_policies
    else PolicySet.from_manifest_inline(_POLICY_FALLBACK)
)

# ── State schema ──────────────────────────────────────────────────────

class ComplianceState(TypedDict):
    input: str             # investigation query
    messages: list[dict]
    output: str            # final investigation report
    next_agent: str        # supervisor routing (ManifestExecutor supervisor pattern)
    specialist_outputs: dict
    specialist_call_counts: dict
    iterations: int
    trace_id: str
    policy_flags: list[str]

# ── Step labels ───────────────────────────────────────────────────────

STEP_LABELS: dict[str, str] = {
    "compliance_planner":       "Compliance Planner deciding next step",
    "graph_investigator":       "Graph Investigator querying the compliance graph",
    "risk_assessor":            "Risk Assessor synthesising compliance findings",
    "tool:find_entity":         "finding entity",
    "tool:get_neighbors":       "retrieving relationships",
    "tool:find_path":           "tracing connection path",
    "tool:get_risk_indicators": "assessing risk indicators",
    "merge":                    "Synthesising investigation findings",
}

# ── Lifespan ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    stats = graph_stats()
    print(
        f"Graph Compliance ready — {stats['node_count']} nodes, {stats['edge_count']} edges | "
        f"LLM: {type(llm).__name__} | "
        f"High-risk entities: {len(stats['high_risk_entities'])} | "
        f"Langfuse: {'connected' if _runtime.langfuse_client else 'disabled'}"
    )
    yield

# ── FastAPI app ───────────────────────────────────────────────────────

app = FastAPI(title="Graph Compliance", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ── Pydantic models ───────────────────────────────────────────────────

class InvestigateRequest(BaseModel):
    query: str

# ── Endpoints ─────────────────────────────────────────────────────────

@app.get("/api/graph/stats")
async def api_graph_stats():
    """Return high-level compliance graph statistics."""
    return graph_stats()


@app.get("/api/graph/entities")
async def api_list_entities():
    """List all entities in the compliance graph."""
    return {"entities": ENTITIES, "count": len(ENTITIES)}


@app.get("/api/graph/network")
async def api_graph_network():
    """Return nodes and edges in vis-network format for the graph canvas."""
    nodes = []
    for e in ENTITIES:
        nodes.append({
            "id": e["id"],
            "label": e["name"],
            "group": e["type"],          # Company / Person / Account
            "risk": e["risk_level"],
            "flags": e.get("flags", []),
            "title": _entity_tooltip(e),  # HTML tooltip shown on hover
            **{k: v for k, v in e.items() if k not in ("id", "name", "type", "risk_level", "flags")},
        })

    edges = []
    for src, dst, rel, attrs in EDGES:
        label = rel.replace("_", " ").title()
        # Shorten OWNS label to include ownership %
        if rel == "OWNS" and "pct" in attrs:
            label = f"OWNS {attrs['pct']}%"
        edges.append({
            "from": src,
            "to": dst,
            "label": label,
            "relationship": rel,
            "attrs": attrs,
        })

    return {"nodes": nodes, "edges": edges}


def _entity_tooltip(e: dict) -> str:
    lines = [f"<b>{e['name']}</b>", f"Type: {e['type']}", f"Risk: {e['risk_level']}"]
    if e.get("country"):
        lines.append(f"Country: {e['country']}")
    if e.get("nationality"):
        lines.append(f"Nationality: {e['nationality']}")
    if e.get("bank"):
        lines.append(f"Bank: {e['bank']}")
    if e.get("flags"):
        lines.append(f"Flags: {', '.join(e['flags'])}")
    return "<br>".join(lines)


@app.get("/api/investigate/stream")
async def investigate_stream(q: str = Query(..., min_length=1)):
    """SSE: stream the compliance investigation step by step."""
    async def generate():
        trace_id = str(uuid.uuid4())
        yield f"data: {json.dumps({'type': 'start', 'trace_id': trace_id})}\n\n"

        pre_eval = await policy_engine.evaluate(
            PolicyStage.PRE_EXECUTION, policies, {"input": q}
        )
        if not pre_eval.allowed:
            blocked = next((r for r in pre_eval.results if not r.passed), None)
            reason = blocked.detail if blocked else "Content policy violation"
            yield f"data: {json.dumps({'type': 'error', 'message': reason})}\n\n"
            return

        initial_state: ComplianceState = {
            "input": q, "messages": [], "output": "",
            "next_agent": "", "specialist_outputs": {}, "specialist_call_counts": {},
            "iterations": 0, "trace_id": trace_id, "policy_flags": [],
        }

        token_queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        executor.set_token_stream(trace_id, token_queue)

        final_state: dict = dict(initial_state)

        async def _run_graph():
            async for chunk in executor.astream(initial_state, stream_mode="updates"):
                if isinstance(chunk, dict):
                    for updates in chunk.values():
                        if isinstance(updates, dict):
                            final_state.update(updates)

        graph_task = asyncio.create_task(_run_graph())

        while not graph_task.done() or not token_queue.empty():
            try:
                item = token_queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.01)
                continue
            if item is None:
                break
            if "reasoning" in item:
                yield f"data: {json.dumps({'type': 'reasoning', 'node': item['node'], 'text': item['reasoning']})}\n\n"
            elif "token" in item:
                yield f"data: {json.dumps({'type': 'token', 'node': item['node'], 'token': item['token']})}\n\n"
            elif item.get("done"):
                node = item["node"]
                label = STEP_LABELS.get(node, node.replace("_", " ").title())
                detail = item.get("detail", {})

                # Supervisor steps carry routing decision + full reasoning text
                if node == "compliance_planner":
                    step_evt = {
                        "type": "planner_step",
                        "node": node,
                        "label": label,
                        "next": detail.get("next", ""),
                        "step": detail.get("step", 0),
                        "reasoning": detail.get("reasoning", ""),
                    }
                # Tool call steps carry query args + graph result
                elif node.startswith("tool:"):
                    tool_name = node[5:]  # strip "tool:"
                    calling_agent = detail.get("calling_agent", "")
                    if calling_agent:
                        agent_display = STEP_LABELS.get(calling_agent, calling_agent.replace("_", " ").title())
                        label = f"{agent_display} — {label}"
                    step_evt = {
                        "type": "tool_step",
                        "node": node,
                        "label": label,
                        "tool": tool_name,
                        "args": detail.get("args", {}),
                        "result": detail.get("result", ""),
                        "judge": detail.get("judge"),
                    }
                # Specialist (investigator / assessor) step carries response preview
                else:
                    step_evt = {
                        "type": "agent_step",
                        "node": node,
                        "label": label,
                        "response": detail.get("response", ""),
                    }
                yield f"data: {json.dumps(step_evt)}\n\n"

        await graph_task

        post_eval = await policy_engine.evaluate(
            PolicyStage.POST_EXECUTION, policies, {"input": q, "output": final_state.get("output", "")}
        )
        output = final_state.get("output", "")
        if post_eval.modified_data and post_eval.modified_data.get("output"):
            output = post_eval.modified_data["output"]

        result = {
            "query": q,
            "report": output,
            "iterations": final_state.get("iterations", 0),
            "trace_id": trace_id,
            "langfuse_url": (
                f"{os.getenv('LANGFUSE_HOST', 'http://localhost:3000')}/traces/{trace_id}"
                if _runtime.langfuse_client else None
            ),
        }
        yield f"data: {json.dumps({'type': 'complete', 'result': result})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/investigate")
async def investigate(body: InvestigateRequest):
    """Batch investigation — runs the full supervisor loop and returns JSON."""
    trace_id = str(uuid.uuid4())
    pre_eval = await policy_engine.evaluate(
        PolicyStage.PRE_EXECUTION, policies, {"input": body.query}
    )
    if not pre_eval.allowed:
        blocked = next((r for r in pre_eval.results if not r.passed), None)
        raise HTTPException(status_code=400, detail=blocked.detail if blocked else "Policy violation")

    state: ComplianceState = {
        "input": body.query, "messages": [], "output": "",
        "next_agent": "", "specialist_outputs": {}, "specialist_call_counts": {},
        "iterations": 0, "trace_id": trace_id, "policy_flags": [],
    }
    result = await executor.ainvoke(state)
    return {
        "query": body.query,
        "report": result.get("output", ""),
        "iterations": result.get("iterations", 0),
        "trace_id": trace_id,
    }


@app.get("/healthz")
async def healthz():
    stats = graph_stats()
    return {"status": "ok", "graph": {"nodes": stats["node_count"], "edges": stats["edge_count"]}, "llm": type(llm).__name__}


@app.get("/")
async def frontend():
    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        return {"message": "Graph Compliance API. POST /api/investigate or GET /api/investigate/stream?q=..."}
    return FileResponse(index)
