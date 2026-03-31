"""Email Assistant — FastAPI backend.

A DSAI demo app that uses AO SDK to classify inbound emails,
route them to specialist agents, draft replies, and enforce policies.

Run:
    cd examples/email_assistant
    uvicorn backend.app:app --reload --port 8001
"""

import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ao.engine.patterns.router import RouterState, build_router
from ao.llm.base import LLMProvider
from ao.policy.engine import PolicyEngine
from ao.policy.schema import PolicySet, PolicyStage

# ── Load env vars ──────────────────────────────────────────────────
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

# ── LLM provider selection ─────────────────────────────────────────


def _create_llm() -> LLMProvider:
    if os.getenv("OPENAI_API_KEY"):
        from ao.llm.openai import OpenAIProvider

        return OpenAIProvider(
            api_key=os.environ["OPENAI_API_KEY"],
            default_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        )
    elif os.getenv("AZURE_OPENAI_ENDPOINT"):
        from ao.llm.azure_openai import AzureOpenAIProvider

        return AzureOpenAIProvider(
            endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
        )
    elif os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_MODEL"):
        from ao.llm.ollama import OllamaProvider

        return OllamaProvider(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            default_model=os.getenv("OLLAMA_MODEL", "gemma3:1b"),
        )
    else:
        raise RuntimeError(
            "No LLM configured. Set OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, or OLLAMA_BASE_URL in .env"
        )


llm = _create_llm()

# ── Policy engine ──────────────────────────────────────────────────

policy_engine = PolicyEngine()
policy_engine.register_builtin_rules()

policies = PolicySet.from_yaml("""
policies:
  - name: content_safety
    stage: post_execution
    action: warn
  - name: pii_filter
    stage: post_execution
    action: redact
""")

# ── In-memory email store (demo) ──────────────────────────────────

SAMPLE_EMAILS = [
    {
        "id": "em-001",
        "from": "angry.customer@example.com",
        "subject": "Broken product received!",
        "body": (
            "My order #12345 arrived completely smashed. The box was torn open "
            "and the product inside is broken beyond repair. This is the second "
            "time this has happened. I want a full refund immediately or I'm "
            "filing a complaint with consumer protection."
        ),
        "status": "new",
        "category": None,
        "draft_reply": None,
    },
    {
        "id": "em-002",
        "from": "jane.doe@company.org",
        "subject": "Return policy question",
        "body": (
            "Hi, I purchased a winter jacket (order #67890) last week but it "
            "doesn't fit well. Could you let me know what your return/exchange "
            "policy is? I'd like to swap it for a size larger if possible."
        ),
        "status": "new",
        "category": None,
        "draft_reply": None,
    },
    {
        "id": "em-003",
        "from": "happy.buyer@gmail.com",
        "subject": "Amazing service!",
        "body": (
            "Just wanted to drop a note to say how impressed I am with your "
            "service. The delivery was lightning fast, packaging was perfect, "
            "and the product quality exceeded expectations. You've earned a "
            "loyal customer. Will definitely recommend to friends!"
        ),
        "status": "new",
        "category": None,
        "draft_reply": None,
    },
    {
        "id": "em-004",
        "from": "tech.lead@startup.io",
        "subject": "API integration help",
        "body": (
            "We're integrating your REST API into our platform and running into "
            "issues with the authentication flow. The OAuth token seems to expire "
            "before our batch job completes. Is there a way to get a longer-lived "
            "token, or should we implement token refresh logic?"
        ),
        "status": "new",
        "category": None,
        "draft_reply": None,
    },
    {
        "id": "em-005",
        "from": "billing@enterprise.com",
        "subject": "Invoice discrepancy",
        "body": (
            "Our accounts team noticed that invoice #INV-2026-0042 shows a charge "
            "of $4,500 but our purchase order was for $3,800. Could you review "
            "this and send a corrected invoice? Our PO number is PO-2026-1187. "
            "Please contact me at john.smith@enterprise.com or call 555-0147."
        ),
        "status": "new",
        "category": None,
        "draft_reply": None,
    },
]

emails_db: dict[str, dict] = {e["id"]: dict(e) for e in SAMPLE_EMAILS}
processing_log: list[dict] = []

# ── Agent functions ────────────────────────────────────────────────


async def classify_email(state: RouterState) -> dict:
    resp = await llm.complete(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an email classifier. Classify the email into exactly "
                    "one of: complaint, inquiry, positive_feedback, technical_support, billing. "
                    "Reply with ONLY the category name, nothing else."
                ),
            },
            {"role": "user", "content": state["input"]},
        ],
        temperature=0.0,
    )
    raw = resp.content.strip().lower().replace(" ", "_")
    valid = {"complaint", "inquiry", "positive_feedback", "technical_support", "billing"}
    route = raw if raw in valid else "inquiry"
    return {
        "route": route,
        "messages": [{"role": "classifier", "content": route}],
    }


async def _draft_reply(state: RouterState, persona: str, temp: float = 0.3) -> dict:
    resp = await llm.complete(
        messages=[
            {"role": "system", "content": persona},
            {"role": "user", "content": state["input"]},
        ],
        temperature=temp,
    )
    return {
        "output": resp.content,
        "messages": [*state["messages"], {"role": "agent", "content": resp.content}],
    }


async def handle_complaint(state: RouterState) -> dict:
    return await _draft_reply(
        state,
        "You are a senior customer service agent handling complaints. "
        "Acknowledge the issue, apologize sincerely, and offer a concrete resolution "
        "(refund, replacement, or escalation). Keep it under 120 words.",
    )


async def handle_inquiry(state: RouterState) -> dict:
    return await _draft_reply(
        state,
        "You are a helpful customer service agent. Answer the customer's question "
        "clearly and concisely. If you don't know the exact answer, offer to connect "
        "them with the right team. Keep it under 100 words.",
    )


async def handle_positive(state: RouterState) -> dict:
    return await _draft_reply(
        state,
        "You are a warm customer service agent responding to positive feedback. "
        "Thank them genuinely and mention you'll share with the team. Keep it under 80 words.",
        temp=0.5,
    )


async def handle_tech_support(state: RouterState) -> dict:
    return await _draft_reply(
        state,
        "You are a technical support engineer. Provide a clear, actionable answer "
        "to the technical question. Include specific steps or documentation links "
        "where helpful. Keep it under 150 words.",
    )


async def handle_billing(state: RouterState) -> dict:
    return await _draft_reply(
        state,
        "You are a billing specialist. Address the billing concern professionally, "
        "confirm you'll review the discrepancy, and provide next steps. "
        "Keep it under 100 words.",
    )


# ── Build LangGraph workflow ──────────────────────────────────────

graph = build_router(
    router_fn=classify_email,
    specialists={
        "complaint": handle_complaint,
        "inquiry": handle_inquiry,
        "positive_feedback": handle_positive,
        "technical_support": handle_tech_support,
        "billing": handle_billing,
    },
)
compiled = graph.compile()

# ── Pydantic models ───────────────────────────────────────────────


class EmailOut(BaseModel):
    id: str
    sender: str  # renamed from 'from' for JSON
    subject: str
    body: str
    status: str
    category: str | None
    draft_reply: str | None


class ProcessResult(BaseModel):
    email_id: str
    category: str
    draft_reply: str
    policy_warnings: list[str]
    tokens_used: int


# ── FastAPI app ───────────────────────────────────────────────────

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    provider_name = type(llm).__name__
    print(f"Email Assistant started — LLM: {provider_name}")
    yield


app = FastAPI(title="Email Assistant", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        )
        for e in emails_db.values()
    ]


@app.get("/api/emails/{email_id}")
async def get_email(email_id: str) -> EmailOut:
    e = emails_db.get(email_id)
    if not e:
        raise HTTPException(404, "Email not found")
    return EmailOut(
        id=e["id"],
        sender=e["from"],
        subject=e["subject"],
        body=e["body"],
        status=e["status"],
        category=e["category"],
        draft_reply=e["draft_reply"],
    )


@app.post("/api/emails/{email_id}/process")
async def process_email(email_id: str) -> ProcessResult:
    e = emails_db.get(email_id)
    if not e:
        raise HTTPException(404, "Email not found")
    if e["status"] == "processed":
        return ProcessResult(
            email_id=email_id,
            category=e["category"],
            draft_reply=e["draft_reply"],
            policy_warnings=[],
            tokens_used=0,
        )

    e["status"] = "processing"

    # Run the LangGraph workflow
    result = await compiled.ainvoke({
        "input": e["body"],
        "route": "",
        "messages": [],
        "output": "",
    })

    # Policy check on output
    eval_result = await policy_engine.evaluate(
        PolicyStage.POST_EXECUTION,
        policies,
        {"output": result["output"]},
    )
    warnings = [r.detail for r in eval_result.results if not r.passed and r.detail]
    draft = eval_result.modified_data.get("output", result["output"])

    e["category"] = result["route"]
    e["draft_reply"] = draft
    e["status"] = "processed"

    log_entry = {
        "email_id": email_id,
        "category": result["route"],
        "tokens": 0,
        "policy_warnings": warnings,
    }
    processing_log.append(log_entry)

    return ProcessResult(
        email_id=email_id,
        category=result["route"],
        draft_reply=draft,
        policy_warnings=warnings,
        tokens_used=0,
    )


@app.post("/api/emails/process-all")
async def process_all_emails() -> list[ProcessResult]:
    results = []
    for email_id in list(emails_db.keys()):
        if emails_db[email_id]["status"] == "new":
            r = await process_email(email_id)
            results.append(r)
    return results


@app.get("/api/stats")
async def get_stats():
    total = len(emails_db)
    processed = sum(1 for e in emails_db.values() if e["status"] == "processed")
    categories = {}
    for e in emails_db.values():
        if e["category"]:
            categories[e["category"]] = categories.get(e["category"], 0) + 1
    return {
        "total_emails": total,
        "processed": processed,
        "pending": total - processed,
        "categories": categories,
        "llm_provider": type(llm).__name__,
    }


# Serve frontend
@app.get("/")
async def serve_index():
    return FileResponse(FRONTEND_DIR / "index.html")


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
