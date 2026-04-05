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
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ao.identity.extract import extract_identity, get_display_name, get_user_id
from ao.memory.long_term import LongTermMemory
from ao.memory.user_memory import UserMemory
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

# ── Long-term vector memory (document RAG) ──────────────────────────────
_memory = LongTermMemory(connection_string=DATABASE_URL, app_id=APP_ID)

# ── Per-user long-term memory (ADR-018) ──────────────────────────────
_user_memory = UserMemory(connection_string=DATABASE_URL, app_id=APP_ID)

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
# compile() is deferred to lifespan() so the PostgreSQL checkpointer (ADR-019) is set up first.
# executor.astream() / ainvoke() use self._compiled internally.

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
    output: str          # synthesised answer (set by supervisor on FINISH)
    sources: list[dict]  # retrieved chunks (set by retrieval_agent tool calls)
    specialist_outputs: dict
    specialist_call_counts: dict
    next_agent: str
    trace_id: str
    policy_flags: list[str]
    _identity: object | None    # IdentityContext injected by endpoint; propagated to tools

# ── Step labels for workflow panel ──────────────────────────────────

RAG_STEP_LABELS: dict[str, str] = {
    "query_analyst":        "Research Coordinator deciding strategy",
    "retrieval_agent":      "Retrieval Agent searching knowledge base",
    "synthesis_agent":      "Synthesis Agent drafting cited answer",
    "tool:vector_search":   "querying document index",
}

# ── Sample documents for demo ────────────────────────────────────────

SAMPLE_DOCS = [
    {
        "title": "Singapore Income Tax Act — Key Provisions",
        "content": (
            "Singapore taxes income on a territorial basis. Residents and companies are taxed "
            "only on income accrued in or derived from Singapore, and on foreign-sourced income "
            "remitted into Singapore (with exemptions for qualifying foreign dividend, branch "
            "profit, and service income). Tax residency for individuals: a person is resident "
            "if physically present or employed in Singapore for 183 days or more in a year. "
            "Chargeable income = gross income minus allowable deductions and reliefs. "
            "Companies incorporated in Singapore are generally treated as tax residents. "
            "The Year of Assessment (YA) corresponds to income earned in the preceding calendar "
            "year. IRAS (Inland Revenue Authority of Singapore) administers income tax under "
            "the Income Tax Act (Cap. 134). Key exemptions include: gains from disposal of "
            "ordinary shares (not taxable as capital gains); certain overseas income under "
            "Section 13(8); start-up tax exemption for first three YAs."
        ),
    },
    {
        "title": "Singapore Corporate Tax Rate and Exemptions",
        "content": (
            "The headline corporate tax rate in Singapore is 17% (flat rate). "
            "Start-up Tax Exemption (SUTE): For the first three YAs, qualifying new companies "
            "get 75% exemption on the first SGD 100,000 of chargeable income and 50% exemption "
            "on the next SGD 100,000. Investment holding companies and property development "
            "companies do not qualify. "
            "Partial Tax Exemption (PTE): For all other companies, 75% exemption on the first "
            "SGD 10,000 of chargeable income and 50% exemption on the next SGD 190,000. "
            "Effective from YA 2020, the PTE cap rose so companies pay 17% only on income "
            "above SGD 200,000 after partial exemption. "
            "Tax rebates may be granted ad-hoc by the government (e.g. COVID-19 rebates). "
            "Withholding tax applies to certain payments to non-residents: 10% on royalties, "
            "15% on interest, 17% on technical service fees."
        ),
    },
    {
        "title": "Singapore GST Registration and Compliance",
        "content": (
            "Goods and Services Tax (GST) is a consumption tax levied on the supply of goods "
            "and services in Singapore and on the import of goods. The GST rate is 9% effective "
            "1 January 2024 (increased from 8%). "
            "Mandatory registration: businesses must register for GST when taxable turnover "
            "exceeds SGD 1 million in a 12-month period (retrospective basis) or is expected "
            "to exceed SGD 1 million in the next 12 months (prospective basis). "
            "Voluntary registration is allowed for businesses below the threshold. "
            "GST-registered businesses charge GST on standard-rated supplies, claim input tax "
            "credits on business purchases, and file GST returns quarterly (or monthly for some). "
            "Zero-rated supplies (0% GST): exported goods, international services. "
            "Exempt supplies: sale/lease of residential properties, financial services. "
            "Late registration penalties: SGD 200 per year plus 10% of tax unpaid. "
            "GST returns must be filed within 1 month after end of each accounting period."
        ),
    },
    {
        "title": "Singapore Individual Income Tax Rates and Reliefs",
        "content": (
            "Singapore resident individuals are taxed at progressive rates. YA 2024 onwards: "
            "0% on first SGD 20,000; 2% on next SGD 10,000; 3.5% on next SGD 10,000; "
            "7% on next SGD 40,000; 11.5% on next SGD 40,000; 15% on next SGD 40,000; "
            "18% on next SGD 40,000; 19% on next SGD 40,000; 19.5% on next SGD 40,000; "
            "20% on next SGD 40,000; 22% on income above SGD 320,000. "
            "Non-resident individuals are taxed at 24% (employment income) or 22% (director fees). "
            "Common reliefs: CPF relief (up to SGD 16,000 for employee; SGD 37,740 for self-employed); "
            "Earned Income Relief (SGD 1,000 up to SGD 8,000 for those 60+); "
            "Spouse Relief (SGD 2,000); Child Relief (SGD 4,000 per qualifying child); "
            "Course fees relief (up to SGD 5,500); NSman relief; "
            "SRS contributions (up to SGD 15,300 for citizens/PRs, SGD 35,700 for foreigners). "
            "Total personal reliefs are capped at SGD 80,000 per YA."
        ),
    },
    {
        "title": "Singapore Property Tax Rates and Assessment",
        "content": (
            "Property tax is levied annually on all properties in Singapore. "
            "Owner-occupied residential properties (YA 2024): 0% on first SGD 8,000 AV; "
            "4% on next SGD 47,000; 6% on next SGD 5,000; 10% on next SGD 10,000; "
            "14% on next SGD 15,000; 20% above SGD 85,000 Annual Value (AV). "
            "Non-owner-occupied residential properties: 12% on first SGD 30,000 AV; "
            "20% on next SGD 15,000; progressive rates up to 36% above SGD 90,000 AV. "
            "Commercial and industrial properties: flat 10% on AV. "
            "Annual Value (AV) = estimated gross annual rent if the property were rented out, "
            "determined by IRAS based on market rental data. "
            "Additional Buyer's Stamp Duty (ABSD): Singaporeans buying 2nd residential property "
            "pay 20% ABSD (from Apr 2023); PRs buying 1st property pay 5%; foreigners pay 60%. "
            "Seller's Stamp Duty (SSD): properties sold within 3 years of purchase attract "
            "SSD at 12% (within 1 year), 8% (within 2 years), 4% (within 3 years)."
        ),
    },
    {
        "title": "Singapore Tax Filing Deadlines, Penalties, and Waivers",
        "content": (
            "Individual income tax: Form B1 (residents) or Form B (self-employed) due by "
            "15 April each year; e-filing deadline is 18 April. "
            "Corporate income tax: Estimated Chargeable Income (ECI) must be filed within "
            "3 months from the company's financial year end. Form C-S (Lite) for companies "
            "with revenue ≤ SGD 200,000; Form C-S for revenue ≤ SGD 5 million; Form C otherwise. "
            "Corporate tax filing deadline is 30 November for the relevant YA. "
            "Late filing penalties: IRAS may issue a Summons and impose a fine of up to SGD 1,000 "
            "and a composition amount. Interest on late payment: 5% per annum on outstanding tax. "
            "Penalty for omission or under-reporting: 200% of tax undercharged. "
            "Penalty for tax evasion: up to 400% of tax evaded plus criminal prosecution. "
            "Waiver considerations: IRAS may waive penalties for first-time offenders, genuine "
            "mistakes, voluntary disclosure, or during extenuating circumstances. "
            "Voluntary Disclosure Programme (VDP): penalties reduced if taxpayer proactively "
            "discloses errors before IRAS initiates an audit."
        ),
    },
    {
        "title": "Singapore Capital Gains vs. Income Distinction",
        "content": (
            "Singapore does not have a capital gains tax. However, gains that are revenue "
            "in nature (i.e. from trading activities) are taxable as income. "
            "The distinction between capital gain and income gain depends on the 'badges of trade': "
            "subject matter of transaction (assets normally held as investments tend to be capital); "
            "frequency of transactions; supplementary work done before sale; "
            "reason for acquisition; length of ownership. "
            "Shares: Gains on disposal of shares are generally capital and not taxable. "
            "However, professionals who trade shares as a business (e.g. trading companies) "
            "are taxed on the gains. IRAS looks at whether the primary purpose was investment "
            "or trading. "
            "Property: Gains from property disposal may be capital or income. Developers are "
            "taxed on profits. Individuals who flip properties frequently may be assessed as traders. "
            "Cryptocurrency: IRAS treats gains from disposal of digital tokens as revenue if "
            "the taxpayer is in the business of trading tokens. "
            "Key cases: the IRAS e-Tax Guide 'Income Tax Treatment of Digital Tokens' (2020) "
            "sets out the framework for token taxation."
        ),
    },
    {
        "title": "Singapore Transfer Pricing Rules and Documentation",
        "content": (
            "Singapore's transfer pricing rules require related-party transactions to be "
            "conducted at arm's length, consistent with the OECD Transfer Pricing Guidelines. "
            "Legislation: Section 34D of the Income Tax Act (enforceable from YA 2019). "
            "IRAS may make adjustments if the actual price differs from the arm's length price, "
            "potentially increasing the taxpayer's income or reducing deductions. "
            "Documentation requirements: taxpayers with related-party transactions exceeding "
            "SGD 15 million per type must maintain contemporaneous transfer pricing documentation. "
            "Documentation includes a Group Master File and a Local File. "
            "Deadline: documentation must be completed by the corporate tax filing due date and "
            "made available to IRAS within 30 days of request. "
            "Advance Pricing Arrangement (APA): taxpayers may apply to IRAS for a unilateral, "
            "bilateral, or multilateral APA to provide certainty on transfer pricing methodology. "
            "Surcharge for non-compliance: additional tax of 5% of the adjustment amount "
            "if documentation requirements are not met. "
            "Country-by-Country Reporting (CbCR): applies to MNC groups with consolidated "
            "group revenue of SGD 1.125 billion or more."
        ),
    },
]

# ── Lifespan: init DB + ingest sample docs ───────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # PostgreSQL checkpointer for durable agent-to-agent state (ADR-019)
    try:
        await executor.setup_pg_checkpointer(DATABASE_URL)
    except Exception as exc:
        logger.warning("PostgreSQL checkpointer unavailable, falling back to MemorySaver: %s", exc)
    executor.compile(state_schema=dict)

    db_ok = False
    try:
        await _memory.initialize()
        db_ok = True
        logger.info("LongTermMemory tables initialised")
    except Exception as exc:
        logger.warning("DB init failed (pgvector may not be available): %s", exc)

    # Per-user long-term memory (ADR-018)
    try:
        await _user_memory.initialize()
    except Exception as exc:
        logger.warning("UserMemory init failed (non-critical): %s", exc)

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

@app.get("/api/me")
async def api_me(request: Request):
    """Return the authenticated user's display name and email.

    Reads the X-MS-CLIENT-PRINCIPAL headers injected by ACA EasyAuth (no
    Token Store required).  Returns ``{"authenticated": false}`` for
    unauthenticated requests so the frontend badge can stay hidden.
    """
    identity = extract_identity(request)
    name, email = get_display_name(identity)
    if not email and not name:
        return {"authenticated": False}
    return {"authenticated": True, "name": name, "email": email}


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
async def search_stream(request: Request, q: str = Query(..., min_length=1)):
    """SSE endpoint: stream the search agent's progress and answer token-by-token."""
    identity = extract_identity(request)

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
            "specialist_outputs": {}, "specialist_call_counts": {},
            "next_agent": "", "trace_id": trace_id, "policy_flags": [],
            "_identity": identity,
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
                label = RAG_STEP_LABELS.get(node, node.replace("_", " ").title())

                if node == "query_analyst":
                    # Supervisor routing decision
                    yield f"data: {json.dumps({'type': 'planner_step', 'node': node, 'label': label, 'next': detail.get('next',''), 'step': detail.get('step',0), 'reasoning': detail.get('reasoning','')})}\n\n"
                elif node.startswith("tool:"):
                    # Tool call with query args + truncated result; prefix label with calling agent name
                    calling_agent = detail.get("calling_agent", "")
                    if calling_agent:
                        agent_display = RAG_STEP_LABELS.get(calling_agent, calling_agent.replace("_", " ").title())
                        label = f"{agent_display} — {label}"
                    yield f"data: {json.dumps({'type': 'tool_step', 'node': node, 'label': label, 'tool': node[5:], 'args': detail.get('args',{}), 'result': detail.get('result','')[:500], 'judge': detail.get('judge')})}\n\n"
                else:
                    # Specialist agent final response
                    yield f"data: {json.dumps({'type': 'agent_step', 'node': node, 'label': label, 'response': detail.get('response','')})}\n\n"

        await graph_task

        # Post-execution policy check
        post_eval = await policy_engine.evaluate(
            PolicyStage.POST_EXECUTION, policies, {"input": q, "output": final_state.get("output", "")}
        )
        output = final_state.get("output", "")
        if post_eval.modified_data and post_eval.modified_data.get("output"):
            output = post_eval.modified_data["output"]

        # Write search observation to per-user long-term memory (ADR-018).
        user_id = get_user_id(identity)
        if output:
            try:
                await _user_memory.remember(
                    user_id=user_id,
                    content=f"Search query: {q}\nAnswer summary: {output[:500]}",
                    agent_name="retrieval_agent",
                    memory_type="observation",
                )
                logger.debug("Wrote search observation to UserMemory for user=%s", user_id)
            except Exception as mem_exc:
                logger.warning("UserMemory write failed (non-critical): %s", mem_exc)

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
async def search(request: Request, body: SearchRequest):
    """Batch search — runs the full graph and returns the result as JSON."""
    identity = extract_identity(request)
    trace_id = str(uuid.uuid4())
    pre_eval = await policy_engine.evaluate(
        PolicyStage.PRE_EXECUTION, policies, {"input": body.query}
    )
    if not pre_eval.allowed:
        blocked = next((r for r in pre_eval.results if not r.passed), None)
        raise HTTPException(status_code=400, detail=blocked.detail if blocked else "Policy violation")

    state: RAGState = {
        "input": body.query, "messages": [], "output": "", "sources": [],
        "specialist_outputs": {}, "specialist_call_counts": {},
        "next_agent": "", "trace_id": trace_id, "policy_flags": [],
        "_identity": identity,
    }
    result = await executor.ainvoke(state)
    output = result.get("output", "")
    user_id = get_user_id(identity)
    if output:
        try:
            await _user_memory.remember(
                user_id=user_id,
                content=f"Search query: {body.query}\nAnswer summary: {output[:500]}",
                agent_name="retrieval_agent",
                memory_type="observation",
            )
        except Exception as mem_exc:
            logger.warning("UserMemory write failed (non-critical): %s", mem_exc)
    return {
        "query": body.query,
        "answer": output,
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
