"""Tax Email Assistant — FastAPI backend (Taxpayer Edition).

Classifies inbound taxpayer emails, looks up the taxpayer record from
PostgreSQL, routes to a specialist agent with SOP-grounded prompts,
enforces guardrail policies, and drafts a reply.

Categories
----------
  filing_extension    — Request to extend the submission deadline
  payment_arrangement — Instalment plan / payment difficulty
  assessment_relief   — Objection or appeal against a tax assessment
  penalty_waiver      — Request to waive a late-filing / late-payment penalty
  general_inquiry     — All other tax questions

Run (from project root):
    uvicorn backend.app:app --reload --port 8001 --app-dir examples/email_assistant
"""

import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from langgraph.graph import END, StateGraph
from psycopg.rows import dict_row
from pydantic import BaseModel

from ao.llm.base import LLMProvider
from ao.policy.engine import PolicyEngine
from ao.policy.schema import PolicySet

# ── Config ─────────────────────────────────────────────────────────
load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=True)
logger = logging.getLogger("tax_email_assistant")
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://ao:localdev@localhost:5432/ao")


# ── LLM ────────────────────────────────────────────────────────────
def _create_llm() -> LLMProvider:
    if os.getenv("OPENAI_API_KEY"):
        from ao.llm.openai import OpenAIProvider
        return OpenAIProvider(
            api_key=os.environ["OPENAI_API_KEY"],
            default_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        )
    if os.getenv("AZURE_OPENAI_ENDPOINT"):
        from ao.llm.azure_openai import AzureOpenAIProvider
        return AzureOpenAIProvider(
            endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
        )
    if os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_MODEL"):
        from ao.llm.ollama import OllamaProvider
        return OllamaProvider(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            default_model=os.getenv("OLLAMA_MODEL", "gemma3:1b"),
        )
    raise RuntimeError("No LLM configured. Set OPENAI_API_KEY in .env")

llm = _create_llm()

# ── Langfuse observability client ──────────────────────────────────
def _create_langfuse_client() -> Any | None:
    pk = os.getenv("LANGFUSE_PUBLIC_KEY")
    sk = os.getenv("LANGFUSE_SECRET_KEY")
    if pk and sk:
        from langfuse import Langfuse
        return Langfuse(
            public_key=pk,
            secret_key=sk,
            host=os.getenv("LANGFUSE_HOST", "http://localhost:3000"),
        )
    return None

langfuse_client = _create_langfuse_client()

# trace_id -> active Langfuse trace object (populated during processing)
_active_traces: dict[str, Any] = {}

# ── Step labels for SSE + Dashboard display ─────────────────────────
STEP_LABELS: dict[str, str] = {
    "lookup_taxpayer":     "Looking up taxpayer record in database",
    "classify":            "Classifying email category",
    "filing_extension":    "Filing Extension agent drafting reply",
    "payment_arrangement": "Payment Arrangement agent reviewing case",
    "assessment_relief":   "Assessment Relief agent reviewing objection",
    "penalty_waiver":      "Penalty Waiver agent checking penalty history",
    "general_inquiry":     "General Inquiry agent composing response",
    "policy_check":        "Applying guardrail policies",
}

# ── Policy engine ───────────────────────────────────────────────────
policy_engine = PolicyEngine()
policy_engine.register_builtin_rules()
policies = PolicySet.from_yaml("""
policies:
  - name: pii_filter
    stage: post_execution
    action: redact
  - name: content_safety
    stage: post_execution
    action: warn
  - name: tax_accuracy
    stage: post_execution
    action: warn
""")

# ── Standard Operating Procedures ──────────────────────────────────
# Embedded into each specialist agent's system prompt.
# Production: load from a knowledge base / RAG pipeline.

SOPS: dict[str, str] = {
    "filing_extension": """
STANDARD OPERATING PROCEDURE — Filing Extension Request
1. Extension must be requested BEFORE the original filing deadline.
2. Maximum extension granted: 30 calendar days per tax year.
3. Only ONE extension per tax year per taxpayer is permitted.
4. Individuals use Form B-Ext; Corporations use Form C-Ext.
5. Late requests (after deadline) require documented grounds (hospitalisation, natural disaster).
6. Acknowledge the request, state the decision/next steps, and provide the form reference.
""",
    "payment_arrangement": """
STANDARD OPERATING PROCEDURE — Payment Arrangement
1. Confirm the outstanding balance from the taxpayer record before responding.
2. Maximum instalment plan duration: 12 months.
3. Minimum monthly instalment: SGD 100.
4. If an ACTIVE plan already exists, offer to amend it — do not create a second plan.
5. Interest accrues at 1.5% per month on the remaining balance during the arrangement.
6. Include a payment reference and remind the taxpayer to retain all receipts.
""",
    "assessment_relief": """
STANDARD OPERATING PROCEDURE — Assessment Objection / Relief
1. Objection window: 30 days from the date of the Notice of Assessment (NOA).
2. Requests received AFTER 30 days require special grounds — flag for senior review.
3. Taxpayer must state specific grounds (arithmetic error, omitted deduction, wrong income, etc.).
4. Required documents: copy of NOA + supporting evidence.
5. Processing time: up to 90 working days after all documents received.
6. Acknowledge receipt, confirm the objection window, list required documents.
""",
    "penalty_waiver": """
STANDARD OPERATING PROCEDURE — Penalty Waiver Request
1. Check the taxpayer's PENALTY COUNT from their record:
   - Count 0-1: Proceed — grant waiver if underlying tax is paid in full.
   - Count 2  : Requires compelling mitigating circumstance (illness, redundancy).
   - Count 3+ : DO NOT make a waiver decision — escalate to supervisor (HITL required).
2. Waiver applies to the PENALTY AMOUNT only; underlying tax remains payable.
3. Do not commit to a waiver outcome without confirming full payment status.
4. Acknowledge the request and state next steps based on the count rule above.
""",
    "general_inquiry": """
STANDARD OPERATING PROCEDURE — General Tax Inquiry
1. Provide accurate, factual information based on known tax regulations.
2. Do NOT give legal or financial advice; recommend professional consultation for complex matters.
3. Address the taxpayer by name if known from the taxpayer record.
4. Be concise, clear, and professional.
5. For specialist topics (objections, penalties), refer the taxpayer to the correct division.
""",
}

VALID_CATEGORIES = set(SOPS.keys())

# ── Graph state ─────────────────────────────────────────────────────

class TaxEmailState(TypedDict):
    email_id: str
    sender: str
    subject: str
    input: str              # full email text
    route: str              # category chosen by classifier
    messages: list[dict]
    output: str             # draft reply
    taxpayer: dict | None   # row from taxpayers table (or None if not found)
    trace_id: str
    policy_flags: list[str]
    hitl_required: bool

# ── DB lookup ───────────────────────────────────────────────────────

_TIN_RE = re.compile(r'\bSG-T\d{3}-\d{4}\b', re.IGNORECASE)

async def _db_lookup_taxpayer(sender_email: str, tin: str | None) -> dict | None:
    try:
        async with await psycopg.AsyncConnection.connect(DATABASE_URL, row_factory=dict_row) as conn:
            if tin:
                cur = await conn.execute(
                    "SELECT * FROM taxpayers WHERE tax_id = %s", (tin.upper(),)
                )
            else:
                cur = await conn.execute(
                    "SELECT * FROM taxpayers WHERE email = %s", (sender_email.lower(),)
                )
            return await cur.fetchone()
    except Exception as exc:
        logger.warning("Taxpayer DB lookup failed: %s", exc)
        return None

# ── Graph nodes ─────────────────────────────────────────────────────

async def node_lookup_taxpayer(state: TaxEmailState) -> dict:
    """Extract TIN from email body (regex) or fall back to sender email lookup."""
    lf_trace = _active_traces.get(state.get("trace_id", ""))
    lf_span = lf_trace.span(name="db-lookup-taxpayer", input={"sender": state["sender"]}) if lf_trace else None
    match = _TIN_RE.search(state["input"])
    tin = match.group(0) if match else None
    taxpayer = await _db_lookup_taxpayer(state["sender"], tin)
    if lf_span:
        lf_span.end(output={"found": taxpayer is not None, "tax_id": taxpayer.get("tax_id") if taxpayer else None})
    return {"taxpayer": taxpayer, "messages": []}

async def node_classify(state: TaxEmailState) -> dict:
    """LLM classifies the email into a tax-authority-specific category."""
    lf_trace = _active_traces.get(state.get("trace_id", ""))
    classify_messages = [
        {
            "role": "system",
            "content": (
                "You are a tax authority email triage classifier.\n"
                "Classify the email into exactly ONE of:\n"
                "  filing_extension    — requesting a deadline extension to file\n"
                "  payment_arrangement — payment plan, instalments, paying difficulties\n"
                "  assessment_relief   — objection or appeal against a tax assessment\n"
                "  penalty_waiver      — requesting a penalty or surcharge to be waived\n"
                "  general_inquiry     — all other tax questions\n\n"
                "Reply with ONLY the category name. Nothing else."
            ),
        },
        {"role": "user", "content": state["input"]},
    ]
    lf_gen = lf_trace.generation(
        name="classify",
        model=getattr(llm, "default_model", "unknown"),
        input=classify_messages,
    ) if lf_trace else None
    resp = await llm.complete(messages=classify_messages, temperature=0.0)
    raw = resp.content.strip().lower().replace(" ", "_")
    route = raw if raw in VALID_CATEGORIES else "general_inquiry"
    if lf_gen:
        lf_gen.end(output=route, usage={"input": resp.usage.get("input_tokens", 0), "output": resp.usage.get("output_tokens", 0)})
    return {
        "route": route,
        "messages": [{"role": "classifier", "content": route}],
    }

def _format_taxpayer_context(tp: dict | None) -> str:
    if not tp:
        return "── Taxpayer record NOT FOUND in database. Proceed cautiously. ──"
    return (
        f"Name          : {tp.get('full_name', '—')}\n"
        f"Tax ID        : {tp.get('tax_id', '—')}\n"
        f"Entity Type   : {tp.get('entity_type', '—')}\n"
        f"Filing Status : {tp.get('filing_status', '—')}\n"
        f"Assessment Yr : {tp.get('assessment_year', '—')}\n"
        f"Assessed Amt  : SGD {float(tp.get('assessed_amount') or 0):,.2f}\n"
        f"Outstanding   : SGD {float(tp.get('outstanding_balance') or 0):,.2f}\n"
        f"Payment Plan  : {'Active' if tp.get('payment_plan_active') else 'None'}\n"
        f"Penalty Count : {tp.get('penalty_count', 0)}\n"
        f"Notes         : {tp.get('notes', '—')}"
    )

async def _specialist(state: TaxEmailState, category: str) -> dict:
    sop = SOPS[category]
    tp_ctx = _format_taxpayer_context(state.get("taxpayer"))
    lf_trace = _active_traces.get(state.get("trace_id", ""))
    specialist_messages = [
        {
            "role": "system",
            "content": (
                f"You are a tax authority officer handling "
                f"{category.replace('_', ' ')} cases.\n\n"
                f"TAXPAYER RECORD FROM DATABASE:\n{tp_ctx}\n\n"
                f"SOP YOU MUST FOLLOW:\n{sop}\n"
                "Draft a professional reply email. Address the taxpayer by name. "
                "Follow the SOP strictly — do not invent policies. "
                "Keep the reply under 200 words."
            ),
        },
        {"role": "user", "content": state["input"]},
    ]
    lf_gen = lf_trace.generation(
        name=f"specialist-{category}",
        model=getattr(llm, "default_model", "unknown"),
        input=specialist_messages,
        metadata={"category": category, "sop_applied": True},
    ) if lf_trace else None
    resp = await llm.complete(messages=specialist_messages, temperature=0.2)
    if lf_gen:
        lf_gen.end(
            output=resp.content,
            usage={"input": resp.usage.get("input_tokens", 0), "output": resp.usage.get("output_tokens", 0)},
        )
    return {
        "output": resp.content,
        "messages": [*state["messages"], {"role": "agent", "content": resp.content}],
    }

async def node_filing_extension(state: TaxEmailState) -> dict:
    return await _specialist(state, "filing_extension")

async def node_payment_arrangement(state: TaxEmailState) -> dict:
    return await _specialist(state, "payment_arrangement")

async def node_assessment_relief(state: TaxEmailState) -> dict:
    return await _specialist(state, "assessment_relief")

async def node_penalty_waiver(state: TaxEmailState) -> dict:
    result = await _specialist(state, "penalty_waiver")
    tp = state.get("taxpayer")
    hitl = bool(tp and (tp.get("penalty_count") or 0) >= 3)
    flags = list(state.get("policy_flags", []))
    if hitl:
        flags.append(
            f"HITL_REQUIRED: {tp.get('tax_id')} has {tp.get('penalty_count')} "
            "penalties — waiver decision requires supervisor approval"
        )
    result["policy_flags"] = flags
    result["hitl_required"] = hitl
    return result

async def node_general_inquiry(state: TaxEmailState) -> dict:
    return await _specialist(state, "general_inquiry")

# ── Build LangGraph ─────────────────────────────────────────────────

def _build_graph() -> StateGraph:
    g = StateGraph(TaxEmailState)
    g.add_node("lookup_taxpayer",     node_lookup_taxpayer)
    g.add_node("classify",            node_classify)
    g.add_node("filing_extension",    node_filing_extension)
    g.add_node("payment_arrangement", node_payment_arrangement)
    g.add_node("assessment_relief",   node_assessment_relief)
    g.add_node("penalty_waiver",      node_penalty_waiver)
    g.add_node("general_inquiry",     node_general_inquiry)
    g.set_entry_point("lookup_taxpayer")
    g.add_edge("lookup_taxpayer", "classify")
    g.add_conditional_edges(
        "classify",
        lambda s: s.get("route", "general_inquiry"),
        {k: k for k in VALID_CATEGORIES},
    )
    for k in VALID_CATEGORIES:
        g.add_edge(k, END)
    return g

compiled_graph = _build_graph().compile()

# ── Sample taxpayer emails ──────────────────────────────────────────

SAMPLE_EMAILS = [
    {
        "id": "em-001",
        "from": "john.tan@example.com",
        "subject": "Request for Filing Extension — SG-T001-2890",
        "body": (
            "Dear Tax Authority,\n\n"
            "I am writing to request a 30-day extension for my income tax filing "
            "for YA 2024. My tax reference is SG-T001-2890. I have been on an "
            "extended overseas work assignment and require additional time to "
            "compile my supporting documents.\n\n"
            "Please advise on the necessary steps and any forms I need to submit.\n\n"
            "Thank you,\nJohn Tan Wei Ming"
        ),
        "status": "new", "category": None, "draft_reply": None, "policy_flags": [],
    },
    {
        "id": "em-002",
        "from": "priya.k@example.com",
        "subject": "Payment Arrangement Amendment — TIN SG-T002-4471",
        "body": (
            "Good afternoon,\n\n"
            "I currently have an active payment arrangement for my outstanding "
            "balance of SGD 3,200 (YA 2024). My monthly instalment is SGD 400 "
            "but due to recent medical expenses I am struggling. May I revise "
            "the monthly payment down to SGD 200?\n\nMy TIN is SG-T002-4471.\n\n"
            "Thank you,\nPriya Krishnan"
        ),
        "status": "new", "category": None, "draft_reply": None, "policy_flags": [],
    },
    {
        "id": "em-003",
        "from": "ahmad.r@gmail.com",
        "subject": "Penalty Waiver Request — SG-T004-9934",
        "body": (
            "To Whom It May Concern,\n\n"
            "I received two penalty notices for late filing. I was hospitalised "
            "for three weeks in March 2025 which prevented me from filing on time. "
            "I have since filed my return and paid the full outstanding tax "
            "of SGD 4,200. My TIN is SG-T004-9934.\n\n"
            "I am respectfully requesting a full waiver of the late-filing penalties.\n\n"
            "Ahmad Bin Razali"
        ),
        "status": "new", "category": None, "draft_reply": None, "policy_flags": [],
    },
    {
        "id": "em-004",
        "from": "wml@enterprise.sg",
        "subject": "Objection to Notice of Assessment — TIN SG-T005-1122",
        "body": (
            "Dear Sir/Madam,\n\n"
            "I wish to formally object to my Notice of Assessment dated 15 February 2026. "
            "The assessed income of SGD 198,000 includes a one-off gain from a property "
            "disposal of SGD 42,000 which I believe is capital in nature and not subject "
            "to income tax. My TIN is SG-T005-1122.\n\n"
            "Please advise on the objection process and required supporting documents.\n\n"
            "Wong Mei Lin"
        ),
        "status": "new", "category": None, "draft_reply": None, "policy_flags": [],
    },
    {
        "id": "em-005",
        "from": "fatimah.h@gmail.com",
        "subject": "Query on CPF Relief Claim — SG-T008-5594",
        "body": (
            "Hi,\n\n"
            "I would like to check if I can claim CPF cash top-up relief for "
            "voluntary contributions made to my parents' CPF Retirement Accounts. "
            "My TIN is SG-T008-5594.\n\n"
            "Also, is the relief cap of SGD 7,000 applied per recipient or as a "
            "combined total across all recipients?\n\nThank you,\nFatimah Hassan"
        ),
        "status": "new", "category": None, "draft_reply": None, "policy_flags": [],
    },
]

emails_db: dict[str, dict] = {e["id"]: dict(e) for e in SAMPLE_EMAILS}
processing_log: list[dict] = []

# ── Pydantic models ─────────────────────────────────────────────────

class EmailOut(BaseModel):
    id: str
    sender: str
    subject: str
    body: str
    status: str
    category: str | None
    draft_reply: str | None
    policy_flags: list[str] = []
    taxpayer_name: str | None = None
    hitl_required: bool = False

class ProcessResult(BaseModel):
    email_id: str
    category: str
    draft_reply: str
    taxpayer_found: bool
    taxpayer_id: str | None
    policy_flags: list[str]
    hitl_required: bool
    trace_id: str

# ── FastAPI app ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    db_ok = False
    try:
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            await conn.execute("SELECT 1")
        db_ok = True
    except Exception as exc:
        logger.warning("PostgreSQL unavailable: %s", exc)
    print(
        f"Tax Email Assistant ready — LLM: {type(llm).__name__}, "
        f"DB: {'connected' if db_ok else 'OFFLINE — taxpayer lookup disabled'}, "
        f"Langfuse: {'connected' if langfuse_client else 'disabled (no keys)'}"
    )
    yield

app = FastAPI(title="Tax Email Assistant", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ── Endpoints ────────────────────────────────────────────────────────

@app.get("/api/emails")
async def list_emails() -> list[EmailOut]:
    return [
        EmailOut(
            id=e["id"],
            sender=e["from"],
            subject=e["subject"],
            body=e["body"],
            status=e["status"],
            category=e["category"],
            draft_reply=e["draft_reply"],
            policy_flags=e.get("policy_flags", []),
            taxpayer_name=e.get("taxpayer_name"),
            hitl_required=e.get("hitl_required", False),
        )
        for e in emails_db.values()
    ]

@app.get("/api/emails/{email_id}/process/stream")
async def process_email_stream(email_id: str):
    """SSE endpoint: streams step-by-step progress as the graph runs."""
    email = emails_db.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    async def generate():
        trace_id = str(uuid.uuid4())
        logger.info("SSE stream starting for email %s (trace %s)", email_id, trace_id)
        full_text = f"From: {email['from']}\nSubject: {email['subject']}\n\n{email['body']}"

        # Open Langfuse trace
        lf_trace = None
        if langfuse_client:
            tp = email.get("taxpayer_name")
            lf_trace = langfuse_client.trace(
                name="process-email",
                id=trace_id,
                input=full_text,
                metadata={"email_id": email_id, "sender": email["from"], "subject": email["subject"]},
            )
        _active_traces[trace_id] = lf_trace

        yield f"data: {json.dumps({'type': 'start', 'trace_id': trace_id})}\n\n"

        initial_state: TaxEmailState = {
            "email_id": email_id, "sender": email["from"], "subject": email["subject"],
            "input": full_text, "route": "", "messages": [], "output": "",
            "taxpayer": None, "trace_id": trace_id, "policy_flags": [], "hitl_required": False,
        }

        try:
            final_state: dict = dict(initial_state)
            async for step in compiled_graph.astream(initial_state, stream_mode="updates"):
                for node_name, updates in step.items():
                    final_state.update(updates)
                    detail: dict = {}
                    if node_name == "lookup_taxpayer":
                        tp = updates.get("taxpayer")
                        detail = {
                            "taxpayer_found": tp is not None,
                            "taxpayer_name": tp.get("full_name") if tp else None,
                            "taxpayer_id": tp.get("tax_id") if tp else None,
                        }
                    elif node_name == "classify":
                        detail = {"category": updates.get("route", "?")}
                    yield f"data: {json.dumps({'type': 'step', 'node': node_name, 'label': STEP_LABELS.get(node_name, node_name), 'detail': detail})}\n\n"

            # Policy check
            yield f"data: {json.dumps({'type': 'step', 'node': 'policy_check', 'label': STEP_LABELS['policy_check'], 'detail': {}})}\n\n"
            flags: list[str] = list(final_state.get("policy_flags", []))
            try:
                pr = await policy_engine.evaluate(
                    content=final_state.get("output", ""), policy_set=policies, stage="post_execution"
                )
                flags.extend(pr.get("warnings", []))
            except Exception:
                pass

            tp = final_state.get("taxpayer")
            hitl = final_state.get("hitl_required", False)
            email["status"] = "processed"
            email["category"] = final_state["route"]
            email["draft_reply"] = final_state["output"]
            email["policy_flags"] = flags
            email["hitl_required"] = hitl
            if tp:
                email["taxpayer_name"] = tp.get("full_name")

            entry = {
                "trace_id": trace_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "email_id": email_id,
                "category": final_state["route"],
                "taxpayer_found": tp is not None,
                "taxpayer_id": tp.get("tax_id") if tp else None,
                "taxpayer_name": tp.get("full_name") if tp else None,
                "policy_flags": flags,
                "hitl_required": hitl,
                "graph_nodes_visited": [m["role"] for m in final_state.get("messages", [])],
                "langfuse_url": f"{os.getenv('LANGFUSE_HOST', 'http://localhost:3000')}/traces/{trace_id}" if langfuse_client else None,
            }
            processing_log.append(entry)

            if lf_trace:
                lf_trace.update(
                    output=final_state.get("output", ""),
                    metadata={"category": final_state["route"], "hitl": hitl, "policy_flags": flags},
                )
            _active_traces.pop(trace_id, None)

            result = {
                "email_id": email_id, "category": final_state["route"],
                "draft_reply": final_state["output"],
                "taxpayer_found": tp is not None,
                "taxpayer_id": tp.get("tax_id") if tp else None,
                "policy_flags": flags, "hitl_required": hitl, "trace_id": trace_id,
                "langfuse_url": entry.get("langfuse_url"),
            }
            yield f"data: {json.dumps({'type': 'complete', 'result': result})}\n\n"
            logger.info("SSE stream complete for email %s → %s", email_id, final_state['route'])

        except Exception as exc:
            logger.exception("SSE stream error for email %s", email_id)
            _active_traces.pop(trace_id, None)
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/emails/{email_id}/process")
async def process_email(email_id: str) -> ProcessResult:
    email = emails_db.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    trace_id = str(uuid.uuid4())
    full_text = f"From: {email['from']}\nSubject: {email['subject']}\n\n{email['body']}"

    # Open Langfuse trace
    lf_trace = None
    if langfuse_client:
        lf_trace = langfuse_client.trace(
            name="process-email",
            id=trace_id,
            input=full_text,
            metadata={"email_id": email_id, "sender": email["from"]},
        )
    _active_traces[trace_id] = lf_trace

    initial_state: TaxEmailState = {
        "email_id": email_id,
        "sender": email["from"],
        "subject": email["subject"],
        "input": full_text,
        "route": "",
        "messages": [],
        "output": "",
        "taxpayer": None,
        "trace_id": trace_id,
        "policy_flags": [],
        "hitl_required": False,
    }

    result = await compiled_graph.ainvoke(initial_state)

    # Guardrail policy pass
    flags: list[str] = list(result.get("policy_flags", []))
    try:
        pr = await policy_engine.evaluate(
            content=result["output"],
            policy_set=policies,
            stage="post_execution",
        )
        flags.extend(pr.get("warnings", []))
    except Exception:
        pass

    tp = result.get("taxpayer")
    hitl = result.get("hitl_required", False)

    email["status"] = "processed"
    email["category"] = result["route"]
    email["draft_reply"] = result["output"]
    email["policy_flags"] = flags
    email["hitl_required"] = hitl
    if tp:
        email["taxpayer_name"] = tp.get("full_name")

    entry = {
        "trace_id": trace_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "email_id": email_id,
        "category": result["route"],
        "taxpayer_found": tp is not None,
        "taxpayer_id": tp.get("tax_id") if tp else None,
        "taxpayer_name": tp.get("full_name") if tp else None,
        "policy_flags": flags,
        "hitl_required": hitl,
        "graph_nodes_visited": [m["role"] for m in result.get("messages", [])],
        "langfuse_url": f"{os.getenv('LANGFUSE_HOST', 'http://localhost:3000')}/traces/{trace_id}" if langfuse_client else None,
    }
    processing_log.append(entry)
    logger.info(
        "Processed %s → %s | taxpayer=%s | hitl=%s | trace=%s",
        email_id, result["route"],
        tp.get("tax_id") if tp else "NOT FOUND",
        hitl, trace_id,
    )

    if lf_trace:
        lf_trace.update(
            output=result.get("output", ""),
            metadata={"category": result["route"], "hitl": hitl, "policy_flags": flags},
        )
    _active_traces.pop(trace_id, None)

    return ProcessResult(
        email_id=email_id,
        category=result["route"],
        draft_reply=result["output"],
        taxpayer_found=tp is not None,
        taxpayer_id=tp.get("tax_id") if tp else None,
        policy_flags=flags,
        hitl_required=hitl,
        trace_id=trace_id,
    )

@app.post("/api/emails/process-all")
async def process_all():
    import asyncio
    ids = [e["id"] for e in emails_db.values() if e["status"] == "new"]
    results = await asyncio.gather(*[process_email(eid) for eid in ids])
    return {"processed": len(results), "results": [r.model_dump() for r in results]}

@app.get("/api/stats")
async def stats():
    processed = [e for e in emails_db.values() if e["status"] == "processed"]
    by_cat: dict[str, int] = {}
    for e in processed:
        c = e.get("category") or "unknown"
        by_cat[c] = by_cat.get(c, 0) + 1
    return {
        "total": len(emails_db),
        "processed": len(processed),
        "pending": len(emails_db) - len(processed),
        "hitl_flagged": sum(1 for e in processed if e.get("hitl_required")),
        "by_category": by_cat,
        "llm_provider": type(llm).__name__,
    }

@app.get("/api/log")
async def get_log():
    """Full trace log — one entry per email processed, including graph nodes visited."""
    return {"entries": processing_log}

@app.get("/api/taxpayer/{tax_id}")
async def get_taxpayer(tax_id: str):
    """Direct taxpayer record lookup."""
    tp = await _db_lookup_taxpayer("", tax_id)
    if not tp:
        raise HTTPException(status_code=404, detail="Taxpayer not found")
    return tp

@app.get("/")
async def frontend():
    return FileResponse(FRONTEND_DIR / "index.html")

@app.get("/{path:path}")
async def static_fallback(path: str):
    target = FRONTEND_DIR / path
    if target.exists() and target.is_file():
        return FileResponse(target)
    raise HTTPException(status_code=404)
