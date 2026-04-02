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
import asyncio
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
from ao.memory.short_term import ShortTermMemory
from ao.policy.engine import PolicyEngine
from ao.policy.schema import PolicySet

# ── Config ─────────────────────────────────────────────────────────
try:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=True)
except IndexError:
    pass  # Running inside a container — env vars injected via ACA secrets
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
MANIFEST_PATH = Path(__file__).parent.parent / "ao-manifest.yaml"
DATABASE_URL    = os.getenv("DATABASE_URL",    "postgresql://ao:localdev@localhost:5432/ao")
PLATFORM_URL    = os.getenv("AO_PLATFORM_URL", "http://localhost:8000")
APP_ID          = "tax_email_assistant"

# Service Bus — dead-letter failed stream runs (no-op locally when not set)
_SERVICEBUS_CONN_STR  = os.getenv("SERVICEBUS_CONNECTION_STRING")
_SERVICEBUS_TOPIC     = os.getenv("SERVICEBUS_DEAD_LETTER_TOPIC", "ao-dead-letter")

# Redis — email state persistence across restarts (no-op locally when not set)
_REDIS_URL = os.getenv("REDIS_URL")
_redis_memory: ShortTermMemory | None = None

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

# ── Supervisor-pattern executor (used for em-008) ────────────────────
_SUPERVISOR_MANIFEST_PATH = Path(__file__).parent.parent / "ao-manifest-supervisor.yaml"
manifest_sv = AppManifest.from_yaml(_SUPERVISOR_MANIFEST_PATH)
executor_sv = ManifestExecutor(manifest_sv, llm=llm, langfuse_client=langfuse_client)

# ── Active stream tracking — email_id -> trace_id ───────────────────
# Used by the /cancel endpoint to look up which executor + trace to stop.
_active_streams: dict[str, str] = {}   # email_id -> trace_id

# ── Step labels for SSE + Dashboard display ─────────────────────────
STEP_LABELS: dict[str, str] = {
    "lookup_taxpayer":     "Looking up taxpayer record in database",
    "classify":            "Classifying email category",
    "intent_classify":     "Detecting all intents in this email",
    "supervisor":          "Supervisor deciding next specialist to invoke",
    "filing_extension":    "Filing Extension agent drafting reply",
    "payment_arrangement": "Payment Arrangement agent reviewing case",
    "assessment_relief":   "Assessment Relief agent reviewing objection",
    "penalty_waiver":      "Penalty Waiver agent checking penalty history",
    "general_inquiry":     "General Inquiry agent composing response",
    "dispatch":            "Running specialist agents in parallel",
    "merge":               "Synthesising specialist responses into one reply",
    "policy_check":        "Applying guardrail policies",
    "tool:lookup_taxpayer": "Agent looking up taxpayer record from database",
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


async def _send_dead_letter(email_id: str, error: str, trace_id: str) -> None:
    """Send a failed stream run to the Service Bus dead-letter topic.

    No-op when SERVICEBUS_CONNECTION_STRING is not set (local dev).
    Called via asyncio.create_task so it never blocks the SSE response.
    """
    if not _SERVICEBUS_CONN_STR:
        return
    import json as _json_dl

    from azure.servicebus import ServiceBusMessage
    from azure.servicebus.aio import ServiceBusClient

    body = _json_dl.dumps({
        "workflow_id": trace_id,
        "step_name": "process_email_stream",
        "email_id": email_id,
        "error": error,
        "retry_count": 0,
    })
    try:
        async with ServiceBusClient.from_connection_string(_SERVICEBUS_CONN_STR) as client:
            async with client.get_topic_sender(topic_name=_SERVICEBUS_TOPIC) as sender:
                await sender.send_messages(ServiceBusMessage(body))
        logger.warning(
            "Dead-lettered failed email stream email_id=%s trace_id=%s", email_id, trace_id
        )
    except Exception:
        logger.exception("Failed to enqueue dead-letter for email %s", email_id)


async def _persist_email_state(email_id: str, email: dict) -> None:
    """Persist processed email state to Redis so it survives an ACA restart."""
    if not _redis_memory:
        return
    try:
        await _redis_memory.set_data(email_id, "state", {
            "status": email.get("status"),
            "category": email.get("category"),
            "draft_reply": email.get("draft_reply"),
            "policy_flags": email.get("policy_flags", []),
            "hitl_required": email.get("hitl_required", False),
            "taxpayer_name": email.get("taxpayer_name"),
            "hitl_request_id": email.get("hitl_request_id"),
            "completed_steps": email.get("completed_steps", []),
        })
    except Exception as exc:
        logger.warning("Could not persist email %s to Redis: %s", email_id, exc)


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

    Kept as a pre-step fallback for the router/concurrent patterns so that
    the taxpayer record is in state for HITL condition evaluation even when
    the LLM uses the tool-calling path.  In the tool-calling path this node
    is skipped (no pre_steps registered) and the tool does the lookup.
    """
    match = _TIN_RE.search(state["input"])
    if not match:
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


async def _tool_lookup_taxpayer(tin: str) -> dict:
    """Tool callable: look up a taxpayer by TIN, return both LLM-facing text and state update."""
    taxpayer = await _db_lookup_taxpayer("", tin)
    return {
        "content": _format_taxpayer_context(taxpayer),
        "state": {
            "taxpayer": taxpayer,
            "_context": _format_taxpayer_context(taxpayer),
        },
    }

_LOOKUP_TAXPAYER_SCHEMA: dict = {
    "name": "lookup_taxpayer",
    "description": (
        "Look up a taxpayer's record from the database using their Tax Identification "
        "Number (TIN). Call this when the email contains a TIN (format: SG-TXXX-XXXX) "
        "and you need taxpayer details such as outstanding balance, penalty count, or "
        "filing status to respond accurately."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tin": {
                "type": "string",
                "description": "The taxpayer's TIN, e.g. SG-T001-2890",
            }
        },
        "required": ["tin"],
    },
}

# ── Wire executor ────────────────────────────────────────────────────
# Register lookup_taxpayer as an LLM-callable tool so specialists decide
# when to fetch taxpayer data rather than forcing a DB hit on every email.
executor.register_tool("lookup_taxpayer", _tool_lookup_taxpayer, _LOOKUP_TAXPAYER_SCHEMA)
compiled_graph = executor.compile(state_schema=TaxEmailState)

# Wire the same tool into the supervisor executor and compile it
executor_sv.register_tool("lookup_taxpayer", _tool_lookup_taxpayer, _LOOKUP_TAXPAYER_SCHEMA)
compiled_graph_sv = executor_sv.compile(state_schema=TaxEmailState)

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
    {
        "id": "em-008",
        "from": "tbk@tbkpte.com",
        "subject": "Assessment Dispute and Payment Query — SG-T007-8823",
        "body": (
            "Dear Tax Authority,\n\n"
            "I am writing on behalf of Tan Boon Kiat Pte Ltd (TIN: SG-T007-8823) "
            "regarding our YA 2024 Notice of Assessment.\n\n"
            "Our assessed income includes SGD 42,000 described as 'miscellaneous income' "
            "which we believe was incorrectly classified — it represents a capital gain "
            "from the disposal of a subsidiary and should not be subject to income tax.\n\n"
            "While we pursue this objection, we acknowledge the undisputed portion of "
            "SGD 9,800 remains outstanding. We would like to arrange an instalment plan "
            "for this amount while the assessment review is in progress, so as not to "
            "incur further interest.\n\n"
            "Please advise on both matters.\n\n"
            "Regards,\nTan Boon Kiat\nDirector, Tan Boon Kiat Pte Ltd"
        ),
        # Uses supervisor pattern: orchestrator routes assessment_relief first,
        # then reviews output and routes to payment_arrangement, then FINISH.
        # Demonstrates sequential reasoning vs concurrent parallel dispatch.
        # show_reasoning: true on specialists → CoT visible in UI.
        "status": "new", "category": None, "draft_reply": None, "policy_flags": [],
        "mode": "supervisor",
    },
]

emails_db: dict[str, dict] = {e["id"]: dict(e) for e in SAMPLE_EMAILS}
processing_log: list[dict] = []

# ── Graph stream collector (used by SSE token-streaming endpoint) ───

async def _collect_graph_stream(
    exec_: ManifestExecutor,
    initial_state: dict,
    final_state: dict,
) -> list[tuple[str, dict]]:
    """Run executor.astream and collect (node_name, updates) pairs.

    Also merges updates into final_state in-place so the SSE generator
    can build the completion payload after the task finishes.
    Returns the ordered list of (node_name, updates) for step-event emission.
    """
    steps: list[tuple[str, dict]] = []
    async for chunk in exec_.astream(initial_state, stream_mode="updates"):
        for node_name, updates in chunk.items():
            final_state.update(updates)
            steps.append((node_name, updates))
    return steps


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
    completed_steps: list[dict] = []

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
    global policies, _redis_memory
    db_ok = False
    try:
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            await conn.execute("SELECT 1")
        db_ok = True
    except Exception as exc:
        logger.warning("PostgreSQL unavailable: %s", exc)

    # ── Redis: init + hydrate in-memory email state from previous run ──
    if _REDIS_URL:
        try:
            _redis_memory = ShortTermMemory(redis_url=_REDIS_URL, ttl=86400)
            restored = 0
            for email_id in list(emails_db.keys()):
                stored = await _redis_memory.get_data(email_id, "state")
                if stored:
                    emails_db[email_id].update(stored)
                    restored += 1
            if restored:
                logger.info("Restored %d email states from Redis", restored)
        except Exception as exc:
            logger.warning("Redis unavailable — email state will not persist across restarts: %s", exc)
            _redis_memory = None

    # Try to load policies from the AO Platform API; fall back to hardcoded defaults
    platform_policies = await _load_policies_from_platform(APP_ID)
    if platform_policies:
        policies = platform_policies
        logger.info("Loaded %d policies from AO Platform", len(policies.policies))
    else:
        logger.info("Using fallback hardcoded policies (%d)", len(policies.policies))

    # ── Redis checkpointer: initialise async context on each executor ──
    _cp_executors: list = []
    if _REDIS_URL:
        try:
            from langgraph.checkpoint.redis.aio import AsyncRedisSaver as _AsyncRedisSaver
            from langgraph.checkpoint.memory import MemorySaver as _MemSaver
            for _exec in (executor, executor_sv):
                if isinstance(_exec._checkpointer, _AsyncRedisSaver):
                    await _exec._checkpointer.__aenter__()
                    _cp_executors.append(_exec)
            logger.info("Redis checkpointer ready (%d executor(s))", len(_cp_executors))
        except Exception as _exc:
            logger.warning("Redis checkpointer setup failed (%s) — falling back to MemorySaver", _exc)
            for _exec in (executor, executor_sv):
                _exec._checkpointer = _MemSaver()

    print(
        f"Tax Email Assistant ready — LLM: {type(llm).__name__}, "
        f"DB: {'connected' if db_ok else 'OFFLINE — taxpayer lookup disabled'}, "
        f"Redis: {'connected' if _redis_memory else 'disabled'}, "
        f"Langfuse: {'connected' if langfuse_client else 'disabled (no keys)'}"
    )
    yield

    # ── Shutdown cleanup ─────────────────────────────────────────────
    for _exec in _cp_executors:
        try:
            await _exec._checkpointer.__aexit__(None, None, None)
        except Exception as _exc:
            logger.warning("Redis checkpointer close failed: %s", _exc)
    if _redis_memory:
        await _redis_memory.close()

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
            completed_steps=e.get("completed_steps", []),
        )
        for e in emails_db.values()
    ]

@app.post("/api/emails/{email_id}/cancel")
async def cancel_email_stream(email_id: str):
    """Cancel an in-progress stream for email_id.

    The executor will stop between node boundaries — the current node always
    completes cleanly, so partial state is always at a consistent checkpoint.
    The email is marked 'interrupted' and its state is persisted to Redis so
    a retry can display what was already completed.
    """
    trace_id = _active_streams.get(email_id)
    if not trace_id:
        raise HTTPException(status_code=404, detail="No active stream for this email")

    email = emails_db.get(email_id)
    if email:
        active_exec = executor_sv if email.get("mode") == "supervisor" else executor
        active_exec.cancel_stream(trace_id)
        email["status"] = "interrupted"
        await _persist_email_state(email_id, email)

    logger.info("Cancel requested for email %s (trace %s)", email_id, trace_id)
    return {"cancelled": True, "email_id": email_id, "trace_id": trace_id}


@app.get("/api/emails/{email_id}/process/stream")
async def process_email_stream(email_id: str):
    """SSE endpoint: streams step-by-step progress as the graph runs.
    Trace lifecycle (Langfuse open/close) is managed by ManifestExecutor.astream().
    """
    email = emails_db.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    if email["status"] not in ("new", "interrupted"):
        raise HTTPException(status_code=409, detail=f"Email is already {email['status']}")

    async def generate():
        trace_id = str(uuid.uuid4())
        resuming = email["status"] == "interrupted"
        logger.info(
            "SSE stream starting for email %s (trace %s, resuming=%s)",
            email_id, trace_id, resuming,
        )
        full_text = f"From: {email['from']}\nSubject: {email['subject']}\n\n{email['body']}"

        yield f"data: {json.dumps({'type': 'start', 'trace_id': trace_id, 'resuming': resuming})}\n\n"

        initial_state: TaxEmailState = {
            "email_id": email_id, "sender": email["from"], "subject": email["subject"],
            "input": full_text, "route": "", "intents": [], "specialist_outputs": {},
            "messages": [], "output": "",
            "taxpayer": None, "_context": "", "trace_id": trace_id,
            "policy_flags": [], "hitl_required": False, "hitl_action": "",
        }

        # ── Token streaming setup ──────────────────────────────────
        # Route supervisor-mode emails to the supervisor executor
        active_executor = executor_sv if email.get("mode") == "supervisor" else executor
        token_queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        active_executor.set_token_stream(trace_id, token_queue)

        # Register so /cancel can signal this run
        email["status"] = "processing"
        _active_streams[email_id] = trace_id

        try:
            final_state: dict = dict(initial_state)
            # Accumulate step events so they can be persisted and restored after reload
            completed_steps_log: list[dict] = []

            # Run graph stream in a background task so we can interleave token events
            graph_task = asyncio.create_task(
                _collect_graph_stream(active_executor, initial_state, final_state)
            )

            # Drain token queue until graph finishes
            streamed_nodes: set[str] = set()
            while not graph_task.done() or not token_queue.empty():
                try:
                    item = token_queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.01)
                    continue

                if item is None:
                    break  # sentinel from executor — stream ended
                if "reasoning" in item:
                    yield f"data: {json.dumps({'type': 'reasoning', 'node': item['node'], 'text': item['reasoning']})}\n\n"
                elif "token" in item:
                    yield f"data: {json.dumps({'type': 'token', 'node': item['node'], 'token': item['token']})}\n\n"
                elif item.get("done"):
                    # Node finished streaming (or tool call completed) — emit step event
                    node_name = item["node"]
                    streamed_nodes.add(node_name)
                    step_evt = {'type': 'step', 'node': node_name, 'label': STEP_LABELS.get(node_name, node_name), 'detail': item.get('detail', {})}
                    completed_steps_log.append({'node': node_name, 'label': step_evt['label'], 'detail': step_evt['detail']})
                    yield f"data: {json.dumps(step_evt)}\n\n"

            # Await graph task and get node-level step events
            graph_steps = await graph_task

            # Detect if the run was cancelled (executor stopped between nodes)
            was_cancelled = active_executor.is_cancelled(trace_id)

            for node_name, updates in graph_steps:
                final_state.update(updates)
                # Skip nodes that already got a step event via the token/tool queue
                if node_name in streamed_nodes:
                    continue
                detail: dict = {}
                if node_name == "lookup_taxpayer":
                    tp = updates.get("taxpayer")
                    detail = {"taxpayer_found": tp is not None, "skipped": tp is None and not updates.get("_context")}
                    if tp:
                        detail.update({"taxpayer_name": tp.get("full_name"), "taxpayer_id": tp.get("tax_id")})
                elif node_name in ("classify", "intent_classify"):
                    detail = {"category": updates.get("route") or ", ".join(updates.get("intents", []))}
                elif node_name == "supervisor":
                    detail = {"next": updates.get("next_agent", "?")}
                elif node_name == "dispatch":
                    detail = {"agents": updates.get("intents", [])}
                elif node_name == "merge":
                    so = updates.get("specialist_outputs") or final_state.get("specialist_outputs", {})
                    detail = {"merged_from": list(so.keys())}
                step_evt2 = {'type': 'step', 'node': node_name, 'label': STEP_LABELS.get(node_name, node_name), 'detail': detail}
                completed_steps_log.append({'node': node_name, 'label': step_evt2['label'], 'detail': step_evt2['detail']})
                yield f"data: {json.dumps(step_evt2)}\n\n"

            # Detect if the run was cancelled between node boundaries
            was_cancelled = active_executor.is_cancelled(trace_id)

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
            logger.info(
                "SSE final_state for email %s — taxpayer=%s intents=%s hitl=%s",
                email_id,
                tp.get("tax_id") if tp else None,
                final_state.get("intents"),
                final_state.get("hitl_required"),
            )
            hitl = final_state.get("hitl_required", False)
            category = _active_category(final_state)

            # ── Cancelled path ─────────────────────────────────────
            if was_cancelled:
                email["status"] = "interrupted"
                email["category"] = category or email.get("category")
                email["policy_flags"] = flags
                # Append synthetic stop step so history is visible after page reload
                completed_steps_log.append({
                    "node": "__stopped__",
                    "label": "\u26d4 Stopped by user",
                    "detail": {"at_step": len(completed_steps_log)},
                })
                email["completed_steps"] = completed_steps_log
                await _persist_email_state(email_id, email)
                logger.info("Stream cancelled for email %s after %d nodes", email_id, len(graph_steps))
                yield f"data: {json.dumps({'type': 'cancelled', 'email_id': email_id, 'nodes_completed': len(graph_steps)})}\n\n"
                return

            # ── Normal completion path ─────────────────────────────
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

            # Persist to Redis so processed state survives a container restart
            await _persist_email_state(email_id, email)

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
            asyncio.create_task(_send_dead_letter(email_id, str(exc), trace_id))
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            _active_streams.pop(email_id, None)

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
    await _persist_email_state(email_id, email)
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
    return FileResponse(
        FRONTEND_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )

@app.get("/{path:path}")
async def static_fallback(path: str):
    target = FRONTEND_DIR / path
    if target.exists() and target.is_file():
        return FileResponse(target)
    raise HTTPException(status_code=404)
