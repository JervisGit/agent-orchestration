"""Tax Email Assistant — FastAPI backend (Taxpayer Edition).

Classifies inbound taxpayer emails, looks up the taxpayer record from
PostgreSQL, routes to a specialist agent with SOP-grounded prompts,
enforces guardrail policies, and drafts a reply.

The workflow (LangGraph graph) is built entirely from ao-manifest.yaml by
ManifestExecutor. This file contains only app-specific code:
  - PostgreSQL taxpayer lookup (node_lookup_taxpayer)
  - Extended state schema (TaxEmailState)
  - FastAPI HTTP + SSE endpoints

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

import httpx
import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from psycopg.rows import dict_row
from pydantic import BaseModel

from ao.config.manifest import AppManifest
from ao.engine.manifest_executor import ManifestExecutor
from ao.llm.base import LLMProvider
from ao.policy.engine import PolicyEngine
from ao.policy.schema import PolicySet

# ── Config ─────────────────────────────────────────────────────────
load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=True)
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
MANIFEST_PATH = Path(__file__).parent.parent / "ao-manifest.yaml"
DATABASE_URL    = os.getenv("DATABASE_URL",    "postgresql://ao:localdev@localhost:5432/ao")
PLATFORM_URL    = os.getenv("AO_PLATFORM_URL", "http://localhost:8000")
APP_ID          = "tax_email_assistant"

# ── Structured JSON logging ─────────────────────────────────────────
def _configure_logging() -> None:
    import sys
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
logger = logging.getLogger("tax_email_assistant")


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

# ── ManifestExecutor ────────────────────────────────────────────────
# Reads ao-manifest.yaml and builds the LangGraph automatically.
# No StateGraph / add_node / add_edge code in this file.
manifest = AppManifest.from_yaml(MANIFEST_PATH)
executor = ManifestExecutor(manifest, llm=llm, langfuse_client=langfuse_client)

# ── Step labels for SSE + Dashboard display ─────────────────────────
STEP_LABELS: dict[str, str] = {
    "lookup_taxpayer":     "Looking up taxpayer record in database",
    "classify":            "Classifying email category",
    "intent_classify":     "Detecting all intents in this email",
    "filing_extension":    "Filing Extension agent drafting reply",
    "payment_arrangement": "Payment Arrangement agent reviewing case",
    "assessment_relief":   "Assessment Relief agent reviewing objection",
    "penalty_waiver":      "Penalty Waiver agent checking penalty history",
    "general_inquiry":     "General Inquiry agent composing response",
    "dispatch":            "Running specialist agents in parallel",
    "merge":               "Synthesising specialist responses into one reply",
    "policy_check":        "Applying guardrail policies",
}

# ── Policy engine ───────────────────────────────────────────────────
policy_engine = PolicyEngine()
policy_engine.register_builtin_rules()

_POLICY_YAML_FALLBACK = """
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
"""
policies: PolicySet = PolicySet.from_yaml(_POLICY_YAML_FALLBACK)


async def _load_policies_from_platform(app_id: str) -> PolicySet | None:
    """Fetch policies from the AO Platform API; return None on any failure."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                f"{PLATFORM_URL}/api/policies/",
                params={"app_id": app_id},
            )
            resp.raise_for_status()
            pols = (resp.json() or {}).get("policies", [])
            if not pols:
                return None
            yaml_lines = ["policies:"]
            for p in pols:
                yaml_lines.append(f"  - name: {p['name']}")
                yaml_lines.append(f"    stage: {p['stage']}")
                yaml_lines.append(f"    action: {p['action']}")
            return PolicySet.from_yaml("\n".join(yaml_lines))
    except Exception as exc:
        logger.warning("Could not load policies from AO Platform: %s", exc)
        return None

# ── App-specific state schema ────────────────────────────────────────

class TaxEmailState(TypedDict):
    email_id: str
    sender: str
    subject: str
    input: str              # full email text
    route: str              # category chosen by classifier (router pattern)
    intents: list[str]      # detected intents (concurrent pattern)
    specialist_outputs: dict  # per-specialist reply text (concurrent pattern)
    messages: list[dict]
    output: str             # draft reply
    taxpayer: dict | None   # row from taxpayers table (or None if not found)
    _context: str           # formatted taxpayer context for specialist prompts
    trace_id: str
    policy_flags: list[str]
    hitl_required: bool
    hitl_action: str        # human-readable description of the action pending approval

# ── DB lookup helpers ────────────────────────────────────────────────

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

def _format_taxpayer_context(tp: dict | None) -> str:
    if not tp:
        return "── Taxpayer record NOT FOUND in database. Proceed cautiously. ──"
    return (
        f"TAXPAYER RECORD FROM DATABASE:\n"
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

# ── Pre-step: DB lookup (app-specific, registered with ManifestExecutor) ─

async def node_lookup_taxpayer(state: TaxEmailState) -> dict:
    """Look up taxpayer record from PostgreSQL.

    Only runs if a TIN (SG-TXXX-XXXX) is present in the email body.
    If no TIN is found, returns an empty context so that agents that
    don't need taxpayer data are not forced to wait on a DB round-trip.
    This keeps DB access agent-driven: an agent that needs the record
    can request the taxpayer to include their TIN.
    """
    match = _TIN_RE.search(state["input"])
    if not match:
        # No TIN in email — skip DB lookup entirely
        logger.debug("No TIN found in email %s — skipping DB lookup", state.get("email_id"))
        return {"taxpayer": None, "_context": "", "messages": []}

    tin = match.group(0)
    lf_trace = executor.get_trace(state.get("trace_id", ""))
    lf_span = lf_trace.span(
        name="db-lookup-taxpayer", input={"sender": state["sender"], "tin": tin}
    ) if lf_trace else None

    taxpayer = await _db_lookup_taxpayer(state["sender"], tin)

    if lf_span:
        try:
            lf_span.end(output={
                "found": taxpayer is not None,
                "tax_id": taxpayer.get("tax_id") if taxpayer else None,
            })
        except Exception:
            pass

    return {
        "taxpayer": taxpayer,
        "_context": _format_taxpayer_context(taxpayer),
        "messages": [],
    }

# ── Wire executor ────────────────────────────────────────────────────
# node_lookup_taxpayer is registered as the only pre-step.
# ManifestExecutor builds classifier + 5 specialist nodes from ao-manifest.yaml.

executor.register_pre_step("lookup_taxpayer", node_lookup_taxpayer)
compiled_graph = executor.compile(state_schema=TaxEmailState)

# ── HITL persistence helpers ──────────────────────────────────────────

def _active_category(state: dict) -> str:
    """Return the routing category from either router ('route') or concurrent ('intents')."""
    intents = state.get("intents") or []
    if intents:
        return ", ".join(intents)
    return state.get("route", "")

async def _persist_hitl_request(
    email: dict,
    final_state: dict,
    trace_id: str,
) -> str | None:
    """Write an ao_hitl_request row to PostgreSQL when HITL is required.

    Returns the new request_id on success, or None if the write fails.
    The payload stores:
    - proposed_action: text from the manifest's hitl_action field
    - taxpayer: taxpayer record used for the decision
    - draft_reply: the AI-generated reply awaiting approval
    - action_webhook: URL the dashboard calls to execute after approval
    """
    request_id = str(uuid.uuid4())
    tp = final_state.get("taxpayer")
    # Resolve {taxpayer_tax_id} placeholder in the hitl_action template
    hitl_action_text = final_state.get("hitl_action", "Review decision")
    if tp:
        hitl_action_text = hitl_action_text.replace("{taxpayer_tax_id}", tp.get("tax_id", "?"))

    payload = {
        "email_id": email.get("id"),
        "sender": email.get("from"),
        "subject": email.get("subject"),
        "proposed_action": hitl_action_text,
        "draft_reply": final_state.get("output", ""),
        "taxpayer_name": tp.get("full_name") if tp else None,
        "taxpayer_tax_id": tp.get("tax_id") if tp else None,
        "taxpayer_penalty_count": tp.get("penalty_count") if tp else None,
        "trace_id": trace_id,
        "action_webhook": f"http://localhost:8001/api/hitl/{request_id}/execute",
    }
    try:
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            await conn.execute(
                """INSERT INTO ao_hitl_requests
                   (request_id, workflow_id, step_name, status, payload)
                   VALUES (%s, %s, %s, 'pending', %s)
                   ON CONFLICT DO NOTHING""",
                (
                    request_id,
                    "email-triage-v1",
                    _active_category(final_state),
                    json.dumps(payload),
                ),
            )
        logger.info("HITL request persisted: %s (email %s)", request_id, email.get("id"))
        return request_id
    except Exception:
        logger.warning("Failed to persist HITL request", exc_info=True)
        return None

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
    {
        "id": "em-006",
        "from": "fatimah.h@gmail.com",
        "subject": "Penalty Waiver Request — SG-T008-5594",
        "body": (
            "Dear IRAS,\n\n"
            "I am writing to request a full waiver of the three late-filing penalties "
            "on my account. I have been dealing with a serious family illness over the "
            "past two years which disrupted my financial management. I have now settled "
            "the full outstanding tax balance of SGD 6,700 and am requesting compassionate "
            "consideration for the penalties.\n\n"
            "My TIN is SG-T008-5594.\n\n"
            "Thank you,\nFatimah bte Hassan"
        ),
        # penalty_count=3 on this account → hitl_condition fires → supervisor approval required
        "status": "new", "category": None, "draft_reply": None, "policy_flags": [],
    },
    {
        "id": "em-007",
        "from": "john.tan@example.com",
        "subject": "Filing Extension and Payment Plan Request — SG-T001-2890",
        "body": (
            "Dear Tax Authority,\n\n"
            "I need to raise two matters regarding my YA 2024 assessment "
            "(TIN: SG-T001-2890).\n\n"
            "First, I am requesting a 30-day filing extension. I was hospitalised "
            "for two weeks and cannot compile my documents before the 15 April 2026 "
            "deadline.\n\n"
            "Second, I would also like to arrange a payment plan for the estimated "
            "outstanding tax of SGD 8,500 spread over 12 monthly instalments, as I "
            "anticipate cashflow difficulties following my medical expenses.\n\n"
            "Could you please address both matters?\n\n"
            "Regards,\nJohn Tan Wei Ming"
        ),
        # Two distinct intents: filing_extension + payment_arrangement.
        # With the 'router' pattern the classifier picks ONE intent and the second
        # is left unanswered — this demonstrates why the 'magentic' pattern exists.
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
    hitl_request_id: str | None = None

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
    global policies
    db_ok = False
    try:
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            await conn.execute("SELECT 1")
        db_ok = True
    except Exception as exc:
        logger.warning("PostgreSQL unavailable: %s", exc)

    # Try to load policies from the AO Platform API; fall back to hardcoded defaults
    platform_policies = await _load_policies_from_platform(APP_ID)
    if platform_policies:
        policies = platform_policies
        logger.info("Loaded %d policies from AO Platform", len(policies.policies))
    else:
        logger.info("Using fallback hardcoded policies (%d)", len(policies.policies))

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
            hitl_request_id=e.get("hitl_request_id"),
        )
        for e in emails_db.values()
    ]

@app.get("/api/emails/{email_id}/process/stream")
async def process_email_stream(email_id: str):
    """SSE endpoint: streams step-by-step progress as the graph runs.
    Trace lifecycle (Langfuse open/close) is managed by ManifestExecutor.astream().
    """
    email = emails_db.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    async def generate():
        trace_id = str(uuid.uuid4())
        logger.info("SSE stream starting for email %s (trace %s)", email_id, trace_id)
        full_text = f"From: {email['from']}\nSubject: {email['subject']}\n\n{email['body']}"

        yield f"data: {json.dumps({'type': 'start', 'trace_id': trace_id})}\n\n"

        initial_state: TaxEmailState = {
            "email_id": email_id, "sender": email["from"], "subject": email["subject"],
            "input": full_text, "route": "", "intents": [], "specialist_outputs": {},
            "messages": [], "output": "",
            "taxpayer": None, "_context": "", "trace_id": trace_id,
            "policy_flags": [], "hitl_required": False, "hitl_action": "",
        }

        try:
            final_state: dict = dict(initial_state)
            async for step in executor.astream(initial_state, stream_mode="updates"):
                for node_name, updates in step.items():
                    final_state.update(updates)
                    detail: dict = {}
                    if node_name == "lookup_taxpayer":
                        tp = updates.get("taxpayer")
                        if tp is None and not updates.get("_context"):
                            # TIN-conditional skip — no DB hit
                            detail = {"taxpayer_found": False, "skipped": True}
                        else:
                            detail = {
                                "taxpayer_found": tp is not None,
                                "taxpayer_name": tp.get("full_name") if tp else None,
                                "taxpayer_id": tp.get("tax_id") if tp else None,
                            }
                    elif node_name in ("classify", "intent_classify"):
                        route = updates.get("route") or ", ".join(updates.get("intents", []))
                        detail = {"category": route or "?"}
                    elif node_name == "dispatch":
                        detail = {"agents": updates.get("intents", [])}
                    elif node_name == "merge":
                        so = updates.get("specialist_outputs") or final_state.get("specialist_outputs", {})
                        detail = {"merged_from": list(so.keys())}
                    yield f"data: {json.dumps({'type': 'step', 'node': node_name, 'label': STEP_LABELS.get(node_name, node_name), 'detail': detail})}\n\n"

            # Post-execution policy check
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
            category = _active_category(final_state)
            email["status"] = "processed"
            email["category"] = category
            email["draft_reply"] = final_state["output"]
            email["policy_flags"] = flags
            email["hitl_required"] = hitl
            if tp:
                email["taxpayer_name"] = tp.get("full_name")

            hitl_request_id: str | None = None
            if hitl:
                hitl_request_id = await _persist_hitl_request(email, final_state, trace_id)
                if hitl_request_id:
                    email["hitl_request_id"] = hitl_request_id

            langfuse_url = (
                f"{os.getenv('LANGFUSE_HOST', 'http://localhost:3000')}/traces/{trace_id}"
                if langfuse_client else None
            )
            entry = {
                "trace_id": trace_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "email_id": email_id,
                "category": category,
                "taxpayer_found": tp is not None,
                "taxpayer_id": tp.get("tax_id") if tp else None,
                "taxpayer_name": tp.get("full_name") if tp else None,
                "policy_flags": flags,
                "hitl_required": hitl,
                "hitl_request_id": hitl_request_id,
                "graph_nodes_visited": [m["role"] for m in final_state.get("messages", [])],
                "langfuse_url": langfuse_url,
            }
            processing_log.append(entry)

            result = {
                "email_id": email_id, "category": category,
                "draft_reply": final_state["output"],
                "taxpayer_found": tp is not None,
                "taxpayer_id": tp.get("tax_id") if tp else None,
                "policy_flags": flags, "hitl_required": hitl,
                "hitl_request_id": hitl_request_id,
                "trace_id": trace_id,
                "langfuse_url": langfuse_url,
            }
            yield f"data: {json.dumps({'type': 'complete', 'result': result})}\n\n"
            logger.info("SSE stream complete for email %s → %s", email_id, category)

        except Exception as exc:
            logger.exception("SSE stream error for email %s", email_id)
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
    initial_state: TaxEmailState = {
        "email_id": email_id, "sender": email["from"], "subject": email["subject"],
        "input": full_text, "route": "", "intents": [], "specialist_outputs": {},
        "messages": [], "output": "",
        "taxpayer": None, "_context": "", "trace_id": trace_id,
        "policy_flags": [], "hitl_required": False, "hitl_action": "",
    }

    result = await executor.ainvoke(initial_state)

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
    category = _active_category(result)

    email["status"] = "processed"
    email["category"] = category
    email["draft_reply"] = result["output"]
    email["policy_flags"] = flags
    email["hitl_required"] = hitl
    if tp:
        email["taxpayer_name"] = tp.get("full_name")

    hitl_request_id: str | None = None
    if hitl:
        hitl_request_id = await _persist_hitl_request(email, result, trace_id)
        if hitl_request_id:
            email["hitl_request_id"] = hitl_request_id

    entry = {
        "trace_id": trace_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "email_id": email_id,
        "category": category,
        "taxpayer_found": tp is not None,
        "taxpayer_id": tp.get("tax_id") if tp else None,
        "taxpayer_name": tp.get("full_name") if tp else None,
        "policy_flags": flags,
        "hitl_required": hitl,
        "hitl_request_id": hitl_request_id,
        "graph_nodes_visited": [m["role"] for m in result.get("messages", [])],
        "langfuse_url": f"{os.getenv('LANGFUSE_HOST', 'http://localhost:3000')}/traces/{trace_id}" if langfuse_client else None,
    }
    processing_log.append(entry)
    logger.info(
        "Processed %s → %s | taxpayer=%s | hitl=%s | trace=%s",
        email_id, category,
        tp.get("tax_id") if tp else "NOT FOUND",
        hitl, trace_id,
    )

    return ProcessResult(
        email_id=email_id,
        category=category,
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


# ── HITL execution endpoint ──────────────────────────────────────────
# Called by the dashboard's Approve button (via payload.action_webhook).
# Executes the approved action (update taxpayer notes) and marks the
# HITL request as executed in ao_hitl_requests.

class HITLResolve(BaseModel):
    approved: bool
    reviewer: str = "dashboard-user"
    note: str = ""

@app.post("/api/hitl/{request_id}/execute")
async def execute_hitl_action(request_id: str, body: HITLResolve):
    """Execute the approved HITL action and mark the request as executed.

    Called by the AO Dashboard after the supervisor clicks Approve.
    On approval: updates the taxpayer record notes and marks the email resolved.
    On rejection: marks as rejected, no DB changes to taxpayer record.
    """
    now = datetime.now(timezone.utc)
    try:
        async with await psycopg.AsyncConnection.connect(DATABASE_URL, row_factory=dict_row) as conn:
            # Fetch the request
            cur = await conn.execute(
                "SELECT * FROM ao_hitl_requests WHERE request_id = %s", (request_id,)
            )
            req = await cur.fetchone()
            if not req:
                raise HTTPException(status_code=404, detail="HITL request not found")

            status = "approved" if body.approved else "rejected"

            payload = req["payload"] if isinstance(req["payload"], dict) else json.loads(req["payload"])
            email_id = payload.get("email_id")
            tax_id = payload.get("taxpayer_tax_id")
            action_text = payload.get("proposed_action", "HITL action approved")
            trace_id = payload.get("trace_id")

            if body.approved:
                if tax_id:
                    note_text = (
                        f"[{now.strftime('%Y-%m-%d')}] Penalty waiver APPROVED by {body.reviewer}. "
                        f"Action: {action_text}"
                    )
                    await conn.execute(
                        "UPDATE taxpayers SET notes = notes || %s WHERE tax_id = %s",
                        (f"\n{note_text}", tax_id),
                    )
                    logger.info("HITL approved: updated taxpayer notes for %s", tax_id)

            # Clear the HITL flag in memory so the banner disappears on next list refresh
            if email_id and email_id in emails_db:
                emails_db[email_id]["hitl_required"] = False
                emails_db[email_id]["hitl_resolved"] = True
                emails_db[email_id]["status"] = "hitl_approved" if body.approved else "hitl_rejected"

            # Append a resolution event to the originating Langfuse trace
            if langfuse_client and trace_id:
                try:
                    lf_trace = langfuse_client.trace(id=trace_id)
                    lf_trace.event(
                        name=f"hitl_{status}",
                        metadata={
                            "reviewer": body.reviewer,
                            "action": action_text,
                            "tax_id": tax_id,
                            "note": body.note,
                            "resolved_at": now.isoformat(),
                        },
                    )
                    langfuse_client.flush()
                except Exception:
                    pass

            # Mark request resolved
            await conn.execute(
                "UPDATE ao_hitl_requests "
                "SET status=%s, reviewer=%s, note=%s, resolved_at=%s "
                "WHERE request_id=%s",
                (status, body.reviewer, body.note, now, request_id),
            )

    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("HITL execute failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    return {"status": status, "request_id": request_id, "executed": body.approved}

@app.get("/healthz")
async def healthz():
    """Health check for ACA liveness/readiness probes."""
    checks: dict[str, str] = {}

    # Database ping
    try:
        async with await psycopg.AsyncConnection.connect(
            DATABASE_URL, connect_timeout=3
        ) as conn:
            await conn.execute("SELECT 1")
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"error: {exc}"

    # LLM ping (lightweight — just checks the provider is reachable)
    try:
        probe = await llm.complete(
            messages=[{"role": "user", "content": "ping"}],
            temperature=0,
            max_tokens=1,
        )
        checks["llm"] = "ok" if probe else "no response"
    except Exception as exc:
        checks["llm"] = f"error: {exc}"

    status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": status, "checks": checks}

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
