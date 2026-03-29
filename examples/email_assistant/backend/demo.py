"""Email Assistant Demo — Linear chain workflow using AO Core.

Demonstrates:
- LangGraphEngine with LinearChain pattern
- Identity (ServiceIdentity mode)
- Policy engine (content safety + token budget)
- Observability (tracing)

Run: python examples/email_assistant/backend/demo.py
Requires: pip install -e ao-core
"""

import asyncio
import logging

from ao.engine.base import WorkflowConfig, WorkflowResult
from ao.engine.langgraph_engine import LangGraphEngine
from ao.engine.patterns.linear import LinearState, build_linear_chain
from ao.identity.context import IdentityContext, IdentityMode
from ao.observability.tracer import AOTracer
from ao.policy.engine import PolicyEngine
from ao.policy.rules.content_safety import check_content_safety
from ao.policy.rules.token_budget import check_token_budget
from ao.policy.schema import PolicySet, PolicyStage

logging.basicConfig(level=logging.INFO, format="%(name)s | %(levelname)s | %(message)s")
logger = logging.getLogger("email_assistant")

# ---------- Define workflow steps ----------

def classify_email(state: LinearState) -> dict:
    """Step 1: Classify the incoming email."""
    text = state["input"].lower()
    if "urgent" in text or "asap" in text:
        category = "urgent"
    elif "invoice" in text or "payment" in text:
        category = "billing"
    else:
        category = "general"

    logger.info("Classified email as: %s", category)
    return {
        "messages": state.get("messages", []) + [
            {"role": "system", "content": f"Email classified as: {category}"}
        ],
        "steps_completed": state.get("steps_completed", []) + ["classify"],
    }


def draft_reply(state: LinearState) -> dict:
    """Step 2: Draft a reply based on classification."""
    messages = state.get("messages", [])
    classification = "general"
    for m in messages:
        if "classified as:" in m.get("content", ""):
            classification = m["content"].split("classified as:")[-1].strip()

    replies = {
        "urgent": "Thank you for your urgent message. Our team has been alerted and will respond within the hour.",
        "billing": "Thank you for your billing enquiry. Our finance team will review and respond within 2 business days.",
        "general": "Thank you for contacting us. We will respond within 3 business days.",
    }
    reply = replies.get(classification, replies["general"])

    logger.info("Drafted reply for category: %s", classification)
    return {
        "output": reply,
        "messages": messages + [{"role": "assistant", "content": reply}],
        "steps_completed": state.get("steps_completed", []) + ["draft_reply"],
    }


# ---------- Setup & Run ----------

async def main():
    # 1. Tracer (console-only, no Langfuse keys needed for demo)
    tracer = AOTracer(service_name="email-assistant", enable_console_export=True)

    # 2. Policy engine
    policy_engine = PolicyEngine()
    policy_engine.register_rule("content_safety", check_content_safety)
    policy_engine.register_rule("token_budget", check_token_budget)

    policies = PolicySet.from_yaml("""
policies:
  - name: content_safety
    stage: pre_execution
    action: block
  - name: token_budget
    stage: runtime
    max_tokens_per_run: 50000
""")

    # 3. Identity (service identity — processing external email)
    identity = IdentityContext(
        mode=IdentityMode.SERVICE,
        tenant_id="demo-tenant",
        managed_identity_client_id="demo-client-id",
    )

    # 4. Build workflow
    graph = build_linear_chain([
        ("classify", classify_email),
        ("draft_reply", draft_reply),
    ])

    # 5. Engine
    engine = LangGraphEngine(tracer=tracer)
    engine.register_graph("email_triage", graph)

    # 6. Pre-execution policy check
    test_input = "Hi, I need urgent help with my account. Please respond ASAP."
    logger.info("=== Email Assistant Demo ===")
    logger.info("Input: %s", test_input)

    pre_eval = await policy_engine.evaluate(
        PolicyStage.PRE_EXECUTION, policies, {"input": test_input}
    )
    if not pre_eval.allowed:
        logger.error("Policy blocked: %s", [r.detail for r in pre_eval.results if not r.passed])
        return

    # 7. Run workflow
    config = WorkflowConfig(
        workflow_id="email_triage",
        identity=identity,
        policies=policies,
        metadata={"source": "demo"},
    )
    result: WorkflowResult = await engine.run(config, {
        "input": test_input,
        "messages": [],
        "output": "",
        "steps_completed": [],
    })

    logger.info("Status: %s", result.status)
    logger.info("Output: %s", result.output.get("output", ""))
    logger.info("Steps: %s", result.output.get("steps_completed", []))

    tracer.flush()


if __name__ == "__main__":
    asyncio.run(main())
