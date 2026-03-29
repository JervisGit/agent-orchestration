"""Phase 2 Demo — Resilience + HITL.

Demonstrates:
- HITL approval flow (auto mode + required mode with programmatic approval)
- Retry policy with simulated transient failure
- Fallback handler for non-critical step
- Checkpointing (in-memory for demo)

Run: python examples/email_assistant/backend/demo_phase2.py
"""

import asyncio
import logging

from ao.engine.base import WorkflowConfig
from ao.engine.langgraph_engine import LangGraphEngine
from ao.engine.patterns.linear import LinearState, build_linear_chain
from ao.hitl.manager import ApprovalMode, HITLManager
from ao.identity.context import IdentityContext, IdentityMode
from ao.observability.tracer import AOTracer
from ao.resilience.fallback import FallbackConfig, FallbackHandler
from ao.resilience.retry import RetryPolicy, with_retry

logging.basicConfig(level=logging.INFO, format="%(name)s | %(levelname)s | %(message)s")
logger = logging.getLogger("phase2_demo")

# ---------- Simulate transient failures ----------

_call_count = 0


@with_retry(RetryPolicy(max_retries=2, base_delay=0.1))
async def flaky_classify(text: str) -> str:
    """Simulates a function that fails once then succeeds (transient error)."""
    global _call_count
    _call_count += 1
    if _call_count == 1:
        raise ConnectionError("Simulated transient API failure")
    return "urgent" if "urgent" in text.lower() else "general"


# ---------- Workflow steps ----------

async def classify_with_retry(state: LinearState) -> dict:
    """Step 1: Classify email (with retry on transient failure)."""
    category = await flaky_classify(state["input"])
    logger.info("Classified (after retry): %s", category)
    return {
        "messages": state.get("messages", []) + [
            {"role": "system", "content": f"Classified as: {category}"}
        ],
        "steps_completed": state.get("steps_completed", []) + ["classify"],
    }


def enrich_context(state: LinearState) -> dict:
    """Step 2: Enrich with context (non-critical, has fallback)."""
    # Simulate failure in enrichment
    raise RuntimeError("External enrichment service unavailable")


def draft_reply(state: LinearState) -> dict:
    """Step 3: Draft reply."""
    messages = state.get("messages", [])
    classification = "general"
    for m in messages:
        if "Classified as:" in m.get("content", ""):
            classification = m["content"].split("Classified as:")[-1].strip()

    reply = {
        "urgent": "URGENT: Our team has been alerted and will respond within the hour.",
        "general": "Thank you for contacting us. We will respond within 3 business days.",
    }.get(classification, "Thank you for your message.")

    return {
        "output": reply,
        "messages": messages + [{"role": "assistant", "content": reply}],
        "steps_completed": state.get("steps_completed", []) + ["draft_reply"],
    }


# ---------- Demo 1: HITL Auto Mode ----------

async def demo_hitl_auto():
    """HITL in auto mode — approvals are skipped automatically."""
    logger.info("\n=== Demo 1: HITL Auto Mode ===")

    hitl = HITLManager(default_mode=ApprovalMode.AUTO)
    engine = LangGraphEngine(hitl_manager=hitl)

    graph = build_linear_chain([
        ("classify", lambda s: {
            "messages": [{"role": "system", "content": "Classified as: general"}],
            "steps_completed": ["classify"],
        }),
        ("draft_reply", draft_reply),
    ])
    engine.register_graph(
        "email_auto",
        graph,
        hitl_steps={"classify": ApprovalMode.AUTO},
    )

    config = WorkflowConfig(
        workflow_id="email_auto",
        hitl_enabled=True,
        identity=IdentityContext(mode=IdentityMode.SERVICE, tenant_id="demo"),
    )
    result = await engine.run(config, {
        "input": "Hello, general question.",
        "messages": [],
        "output": "",
        "steps_completed": [],
    })
    logger.info("Result: status=%s output=%s", result.status, result.output.get("output", ""))


# ---------- Demo 2: HITL Required Mode (programmatic approval) ----------

async def demo_hitl_required():
    """HITL in required mode — a simulated reviewer approves the request."""
    logger.info("\n=== Demo 2: HITL Required Mode ===")

    hitl = HITLManager(default_mode=ApprovalMode.REQUIRED, timeout_seconds=5.0)
    engine = LangGraphEngine(hitl_manager=hitl)

    graph = build_linear_chain([
        ("classify", lambda s: {
            "messages": [{"role": "system", "content": "Classified as: urgent"}],
            "steps_completed": ["classify"],
        }),
        ("draft_reply", draft_reply),
    ])
    engine.register_graph(
        "email_hitl",
        graph,
        hitl_steps={"classify": ApprovalMode.REQUIRED},
    )

    config = WorkflowConfig(
        workflow_id="email_hitl",
        hitl_enabled=True,
        identity=IdentityContext(mode=IdentityMode.SERVICE, tenant_id="demo"),
    )

    # Simulate a reviewer approving after 1 second
    async def simulate_reviewer():
        await asyncio.sleep(1.0)
        pending = hitl.pending_requests
        if pending:
            hitl.resolve(pending[0].id, approved=True, reviewer="alice@company.com", note="Looks good")

    # Run workflow and reviewer concurrently
    reviewer_task = asyncio.create_task(simulate_reviewer())
    result = await engine.run(config, {
        "input": "URGENT: Need help now!",
        "messages": [],
        "output": "",
        "steps_completed": [],
    })
    await reviewer_task

    logger.info("Result: status=%s output=%s", result.status, result.output.get("output", ""))


# ---------- Demo 3: Retry on Transient Failure ----------

async def demo_retry():
    """Retry policy recovers from a transient failure."""
    logger.info("\n=== Demo 3: Retry on Transient Failure ===")

    global _call_count
    _call_count = 0

    # Just test the retry decorator directly
    result = await flaky_classify("Urgent request")
    logger.info("Classify result after retry: %s", result)


# ---------- Demo 4: Fallback on Non-Critical Step ----------

async def demo_fallback():
    """Fallback provides default output when a non-critical step fails."""
    logger.info("\n=== Demo 4: Fallback Handler ===")

    fallback = FallbackHandler()
    fallback.register("enrich", FallbackConfig(
        enabled=True,
        default_output={
            "messages": [{"role": "system", "content": "Enrichment skipped (service unavailable)"}],
            "steps_completed": ["enrich_fallback"],
        },
    ))

    # Simulate the enrichment failure + fallback
    try:
        enrich_context({"input": "test", "messages": [], "output": "", "steps_completed": []})
    except RuntimeError as e:
        if fallback.has_fallback("enrich"):
            result = fallback.get_fallback_output("enrich", e, {})
            logger.info("Fallback activated: %s", result)
        else:
            raise


# ---------- Main ----------

async def main():
    await demo_hitl_auto()
    await demo_hitl_required()
    await demo_retry()
    await demo_fallback()
    logger.info("\n=== All Phase 2 demos completed ===")


if __name__ == "__main__":
    asyncio.run(main())
