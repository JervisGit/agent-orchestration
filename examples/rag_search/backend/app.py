"""RAG Search — FastAPI backend.

Demonstrates the **linear** pattern: a single search_agent uses the
vector_search tool to retrieve relevant document chunks, then synthesises a
cited answer.  Documents are stored in PostgreSQL with pgvector embeddings
(ao.memory.long_term.LongTermMemory).

Run (from project root):
    uvicorn backend.app:app --reload --port 8002 --app-dir examples/rag_search

Ingest a document:
    POST /api/documents  {"title": "...", "content": "...", "namespace": "docs"}

Search:
    GET  /api/search/stream?q=your+question   (SSE streaming)
    POST /api/search                          (batch, returns JSON)
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

from ao.memory.long_term import LongTermMemory
from ao.policy.engine import PolicyEngine
from ao.policy.schema import PolicySet, PolicyStage
from ao.runtime import AppRuntime

# ── Config ───────────────────────────────────────────────────────────
try:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=True)
except (IndexError, OSError):
    pass

FRONTEND_DIR   = Path(__file__).parent.parent / "frontend"
MANIFEST_PATH  = Path(__file__).parent.parent / "ao-manifest.yaml"
DATABASE_URL   = os.getenv("DATABASE_URL", "postgresql://ao:localdev@localhost:5432/ao")
APP_ID         = "rag_search"
EMBED_MODEL    = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# ── Structured logging ───────────────────────────────────────────────
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
logger = logging.getLogger("rag_search")

# ── AppRuntime — LLM + Langfuse + ManifestExecutor in one call ────────
_runtime = AppRuntime.from_env(MANIFEST_PATH)
executor = _runtime.executor
llm      = _runtime.llm

# ── Long-term vector memory ──────────────────────────────────────────
_memory = LongTermMemory(connection_string=DATABASE_URL, app_id=APP_ID)

# ── Embedding helper ─────────────────────────────────────────────────

async def _embed(text: str) -> list[float] | None:
    """Generate an embedding vector.  Returns None if embedding is unavailable."""
    try:
        if os.getenv("OPENAI_API_KEY"):
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
            resp = await client.embeddings.create(model=EMBED_MODEL, input=text[:8000])
            return resp.data[0].embedding
        if os.getenv("AZURE_OPENAI_ENDPOINT"):
            from openai import AsyncAzureOpenAI
            client = AsyncAzureOpenAI(
                azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                api_key=os.getenv("AZURE_OPENAI_API_KEY"),
                api_version="2024-02-01",
            )
            resp = await client.embeddings.create(
                model=os.getenv("AZURE_EMBED_DEPLOYMENT", "text-embedding-3-small"),
                input=text[:8000],
            )
            return resp.data[0].embedding
        logger.warning("No embedding provider configured — vector search unavailable")
        return None
    except Exception as exc:
        logger.warning("Embedding failed: %s", exc)
        return None

# ── vector_search tool (pgvector-backed) ────────────────────────────

async def _tool_vector_search(query: str, top_k: int = 5) -> dict:
    """Search the knowledge base for document chunks relevant to the query."""
    top_k = max(1, min(top_k, 10))
    embedding = await _embed(query)
    if embedding is None:
        # Fall back to keyword scan if embedding unavailable
        results = await _memory_keyword_search(query, top_k)
    else:
        results = await _memory.search_similar(embedding, namespace="docs", top_k=top_k)

    if not results:
        return {"content": "No relevant documents found in the knowledge base.", "state": {"sources": []}}

    chunks: list[str] = []
    for i, r in enumerate(results, 1):
        val = r.get("value", {}) if isinstance(r.get("value"), dict) else {"content": str(r.get("value", ""))}
        title   = val.get("title", r.get("key", f"Document {i}"))
        content = val.get("content", "")
        sim     = r.get("similarity")
        sim_str = f" (relevance: {sim:.0%})" if sim is not None else ""
        chunks.append(f"[{i}] **{title}**{sim_str}\n{content}")

    return {
        "content": "\n\n".join(chunks),
        "state": {"sources": results},
    }

async def _memory_keyword_search(query: str, top_k: int) -> list[dict]:
    """Simple fallback: retrieve all docs and return first top_k (no ranking)."""
    try:
        import psycopg
        from psycopg.rows import dict_row
        async with await psycopg.AsyncConnection.connect(DATABASE_URL, row_factory=dict_row) as conn:
            cur = await conn.execute(
                "SELECT key, value FROM ao_long_term_memory"
                " WHERE app_id = %s AND namespace = 'docs'"
                " LIMIT %s",
                (APP_ID, top_k),
            )
            rows = await cur.fetchall()
        return [{"key": r["key"], "value": r["value"], "similarity": None} for r in rows]
    except Exception as exc:
        logger.warning("Keyword fallback search failed: %s", exc)
        return []

_VECTOR_SEARCH_SCHEMA: dict = {
    "name": "vector_search",
    "description": (
        "Search the internal knowledge base for document chunks relevant to a query. "
        "Use this tool whenever you need to find information to answer a question. "
        "Returns the top matching document chunks with titles for citation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query — rephrase the user question to optimise retrieval",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (1–10, default 5)",
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": ["query"],
    },
}

# ── Wire executor ────────────────────────────────────────────────────
executor.register_tool("vector_search", _tool_vector_search, _VECTOR_SEARCH_SCHEMA)
compiled_graph = executor.compile(state_schema=dict)  # generic dict state for linear

# ── Policy engine ────────────────────────────────────────────────────
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

# ── State schema ─────────────────────────────────────────────────────

class RAGState(TypedDict):
    input: str           # user question (executor reads this key)
    messages: list[dict]
    output: str          # synthesised answer
    sources: list[dict]  # retrieved chunks
    trace_id: str
    policy_flags: list[str]

# ── Sample documents for demo ────────────────────────────────────────

SAMPLE_DOCS = [
    {
        "title": "Remote Work Policy",
        "content": (
            "Employees may work remotely up to 3 days per week with manager approval. "
            "Core working hours are 10:00–16:00 SGT. All remote workers must use a VPN "
            "and ensure their home workspace meets security requirements. Expenses for "
            "home office equipment up to SGD 500 per year are reimbursable upon submission "
            "of receipts. Extended remote arrangements (>3 months) require HR approval."
        ),
    },
    {
        "title": "Annual Leave Policy",
        "content": (
            "Full-time employees receive 18 days of annual leave per year, accruing from "
            "the first day of employment. Leave must be applied for at least 3 working days "
            "in advance via the HR system. Unused leave of up to 10 days may be carried "
            "forward to the next calendar year. Leave balance is pro-rated for partial-year "
            "employees. Public holidays are separate from annual leave."
        ),
    },
    {
        "title": "IT Security Policy",
        "content": (
            "All staff must use multi-factor authentication (MFA) for corporate systems. "
            "Passwords must be at least 14 characters and rotated every 90 days. "
            "Personal devices may not store corporate data unless enrolled in MDM. "
            "Suspicious emails should be reported to security@company.sg. "
            "Data classified SECRET or above must not be stored on cloud storage without "
            "encryption. Security incidents must be reported within 1 hour of discovery."
        ),
    },
    {
        "title": "Travel and Expense Policy",
        "content": (
            "Business travel requires pre-approval from the department head. Economy class "
            "is mandated for flights under 6 hours; business class requires VP approval. "
            "Hotel accommodation is capped at SGD 250/night in Singapore and SGD 300/night "
            "overseas. All expenses must be submitted within 30 days via the expense portal "
            "with original receipts. Meals are reimbursed at cost up to SGD 60/day."
        ),
    },
    {
        "title": "Performance Review Process",
        "content": (
            "Formal performance reviews are conducted twice yearly in June and December. "
            "Reviews consist of a self-assessment, manager assessment, and a calibration "
            "discussion. Ratings are: Exceptional, Exceeds Expectations, Meets Expectations, "
            "Needs Improvement. Salary adjustments are linked to the December review. "
            "Employees who receive Needs Improvement must complete a Performance Improvement "
            "Plan (PIP) with clear milestones within 30 days."
        ),
    },
    {
        "title": "Procurement Policy",
        "content": (
            "Purchases under SGD 5,000 may be approved at department level. "
            "Purchases between SGD 5,000 and SGD 50,000 require Finance approval. "
            "Purchases above SGD 50,000 require three competitive quotes and Board approval. "
            "All vendors must be registered in the approved vendor list before purchase orders "
            "can be raised. Preferred payment terms are NET-30 for local vendors."
        ),
    },
]

# ── Lifespan: init DB + ingest sample docs ───────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    db_ok = False
    try:
        await _memory.initialize()
        db_ok = True
        logger.info("LongTermMemory tables initialised")
    except Exception as exc:
        logger.warning("DB init failed (pgvector may not be available): %s", exc)

    # Ingest sample docs on first run (skip if already present)
    if db_ok:
        ingested = 0
        for doc in SAMPLE_DOCS:
            key = doc["title"].lower().replace(" ", "_")
            existing = await _memory.retrieve(key, namespace="docs")
            if existing is None:
                embedding = await _embed(doc["content"])
                await _memory.store(key, doc, namespace="docs", embedding=embedding)
                ingested += 1
        if ingested:
            logger.info("Ingested %d sample documents into knowledge base", ingested)
        else:
            logger.info("Sample documents already present — skipping ingestion")

    print(
        f"RAG Search ready — LLM: {type(llm).__name__}, "
        f"DB: {'connected' if db_ok else 'OFFLINE — fallback mode'}, "
        f"Langfuse: {'connected' if _runtime.langfuse_client else 'disabled'}"
    )
    yield

# ── FastAPI app ───────────────────────────────────────────────────────

app = FastAPI(title="RAG Search", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ── Pydantic models ───────────────────────────────────────────────────

class DocumentIn(BaseModel):
    title: str
    content: str
    namespace: str = "docs"

class SearchRequest(BaseModel):
    query: str

# ── Endpoints ─────────────────────────────────────────────────────────

@app.post("/api/documents")
async def ingest_document(doc: DocumentIn):
    """Ingest a document into the vector knowledge base."""
    key = doc.title.lower().replace(" ", "_")
    embedding = await _embed(doc.content)
    await _memory.store(
        key, {"title": doc.title, "content": doc.content},
        namespace=doc.namespace,
        embedding=embedding,
    )
    logger.info("Ingested document: %s (embedding=%s)", doc.title, embedding is not None)
    return {"key": key, "embedded": embedding is not None}


@app.get("/api/documents")
async def list_documents():
    """List all documents in the knowledge base."""
    try:
        import psycopg
        from psycopg.rows import dict_row
        async with await psycopg.AsyncConnection.connect(DATABASE_URL, row_factory=dict_row) as conn:
            cur = await conn.execute(
                "SELECT key, value->>'title' AS title, created_at"
                " FROM ao_long_term_memory WHERE app_id = %s AND namespace = 'docs'"
                " ORDER BY created_at DESC",
                (APP_ID,),
            )
            rows = await cur.fetchall()
        return {"documents": rows, "count": len(rows)}
    except Exception as exc:
        logger.warning("Could not list documents: %s", exc)
        return {"documents": [], "count": 0}


@app.get("/api/search/stream")
async def search_stream(q: str = Query(..., min_length=1)):
    """SSE endpoint: stream the search agent's progress and answer token-by-token."""
    async def generate():
        trace_id = str(uuid.uuid4())
        yield f"data: {json.dumps({'type': 'start', 'trace_id': trace_id})}\n\n"

        # Pre-execution safety check
        pre_eval = await policy_engine.evaluate(
            PolicyStage.PRE_EXECUTION, policies, {"input": q}
        )
        if not pre_eval.allowed:
            blocked = next((r for r in pre_eval.results if not r.passed), None)
            reason = blocked.detail if blocked else "Content policy violation"
            yield f"data: {json.dumps({'type': 'error', 'message': reason})}\n\n"
            return

        initial_state: RAGState = {
            "input": q, "messages": [], "output": "", "sources": [],
            "trace_id": trace_id, "policy_flags": [],
        }

        token_queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        executor.set_token_stream(trace_id, token_queue)

        final_state: dict = dict(initial_state)

        async def _run_graph():
            async for chunk in executor.astream(initial_state, stream_mode="updates"):
                if isinstance(chunk, dict):
                    for updates in chunk.values():
                        if isinstance(updates, dict):
                            final_state.update(updates)

        graph_task = asyncio.create_task(_run_graph())

        # Stream tool + token events
        while not graph_task.done() or not token_queue.empty():
            try:
                item = token_queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.01)
                continue
            if item is None:
                break
            if "token" in item:
                yield f"data: {json.dumps({'type': 'token', 'node': item['node'], 'token': item['token']})}\n\n"
            elif item.get("done"):
                node = item["node"]
                detail = item.get("detail", {})
                label = "Searching knowledge base" if node.startswith("tool:") else "Synthesising answer"
                yield f"data: {json.dumps({'type': 'step', 'node': node, 'label': label, 'detail': detail})}\n\n"

        await graph_task

        # Post-execution policy check
        post_eval = await policy_engine.evaluate(
            PolicyStage.POST_EXECUTION, policies, {"input": q, "output": final_state.get("output", "")}
        )
        output = final_state.get("output", "")
        if post_eval.modified_data and post_eval.modified_data.get("output"):
            output = post_eval.modified_data["output"]

        sources = final_state.get("sources", [])
        result = {
            "query": q,
            "answer": output,
            "sources": [
                {
                    "title": (s.get("value") or {}).get("title", s.get("key", "Unknown")),
                    "similarity": s.get("similarity"),
                }
                for s in sources
            ],
            "trace_id": trace_id,
        }
        yield f"data: {json.dumps({'type': 'complete', 'result': result})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/search")
async def search(body: SearchRequest):
    """Batch search — runs the full graph and returns the result as JSON."""
    trace_id = str(uuid.uuid4())
    pre_eval = await policy_engine.evaluate(
        PolicyStage.PRE_EXECUTION, policies, {"input": body.query}
    )
    if not pre_eval.allowed:
        blocked = next((r for r in pre_eval.results if not r.passed), None)
        raise HTTPException(status_code=400, detail=blocked.detail if blocked else "Policy violation")

    state: RAGState = {
        "input": body.query, "messages": [], "output": "", "sources": [],
        "trace_id": trace_id, "policy_flags": [],
    }
    result = await executor.ainvoke(state)
    return {
        "query": body.query,
        "answer": result.get("output", ""),
        "sources": result.get("sources", []),
        "trace_id": trace_id,
    }


@app.get("/healthz")
async def healthz():
    checks: dict[str, str] = {}
    try:
        import psycopg
        def _ping():
            with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
        await asyncio.to_thread(_ping)
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"error: {exc}"
    checks["llm"] = type(llm).__name__
    status = "ok" if checks.get("db") == "ok" else "degraded"
    return {"status": status, "checks": checks}


@app.get("/")
async def frontend():
    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        return {"message": "RAG Search API. POST /api/documents to ingest, GET /api/search/stream?q=... to query."}
    return FileResponse(index)
