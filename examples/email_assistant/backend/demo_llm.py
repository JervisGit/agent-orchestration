"""Demo: Real LLM — Email Assistant with Ollama.

Prerequisite:
    ollama pull llama3.2        (or any model you prefer)
    ollama serve                (if not already running)

Run:
    python examples/email_assistant/backend/demo_llm.py

This demo connects the AO SDK to a real LLM via Ollama and runs:
1. Router pattern — LLM classifies email → routes to specialist
2. Specialist drafts a reply using the LLM
3. Policy engine checks the output for PII / safety
4. Traces logged (console) with token usage
"""

import asyncio
import logging
import os

from ao.engine.patterns.router import RouterState, build_router
from ao.llm.ollama import OllamaProvider
from ao.policy.engine import PolicyEngine
from ao.policy.schema import PolicySet, PolicyStage

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")
logger = logging.getLogger("demo_llm")

# ── Config ─────────────────────────────────────────────────────────

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma3:1b")

llm = OllamaProvider(base_url=OLLAMA_BASE_URL, default_model=OLLAMA_MODEL)

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


# ── Router node: LLM classifies the email ─────────────────────────


async def classify_email(state: RouterState) -> dict:
    """Ask the LLM to classify the email into a category."""
    resp = await llm.complete(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an email classifier. Classify the email into exactly one of these categories: "
                    "complaint, inquiry, positive_feedback. "
                    "Reply with ONLY the category name, nothing else."
                ),
            },
            {"role": "user", "content": state["input"]},
        ],
        temperature=0.0,
    )
    category = resp.content.strip().lower().replace(" ", "_")
    # Normalize to known routes
    if "complaint" in category:
        route = "complaint"
    elif "positive" in category or "feedback" in category:
        route = "positive_feedback"
    else:
        route = "inquiry"

    logger.info("Classified as: %s (raw: %s, tokens: %s)", route, resp.content.strip(), resp.usage)
    return {
        "route": route,
        "messages": [{"role": "classifier", "content": route}],
    }


# ── Specialist nodes: LLM drafts reply per category ───────────────


async def handle_complaint(state: RouterState) -> dict:
    resp = await llm.complete(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a customer service agent handling complaints. "
                    "Draft a professional, empathetic reply to the customer's email. "
                    "Keep it under 100 words."
                ),
            },
            {"role": "user", "content": state["input"]},
        ],
        temperature=0.3,
    )
    logger.info("Complaint reply drafted (%d tokens)", resp.usage.get("completion_tokens", 0))
    return {"output": resp.content, "messages": [*state["messages"], {"role": "complaint_agent", "content": resp.content}]}


async def handle_inquiry(state: RouterState) -> dict:
    resp = await llm.complete(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a helpful customer service agent answering inquiries. "
                    "Provide a clear, concise answer. Keep it under 100 words."
                ),
            },
            {"role": "user", "content": state["input"]},
        ],
        temperature=0.3,
    )
    logger.info("Inquiry reply drafted (%d tokens)", resp.usage.get("completion_tokens", 0))
    return {"output": resp.content, "messages": [*state["messages"], {"role": "inquiry_agent", "content": resp.content}]}


async def handle_positive(state: RouterState) -> dict:
    resp = await llm.complete(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a customer service agent responding to positive feedback. "
                    "Thank the customer warmly. Keep it under 80 words."
                ),
            },
            {"role": "user", "content": state["input"]},
        ],
        temperature=0.5,
    )
    logger.info("Positive reply drafted (%d tokens)", resp.usage.get("completion_tokens", 0))
    return {"output": resp.content, "messages": [*state["messages"], {"role": "positive_agent", "content": resp.content}]}


# ── Build the graph ────────────────────────────────────────────────

graph = build_router(
    router_fn=classify_email,
    specialists={
        "complaint": handle_complaint,
        "inquiry": handle_inquiry,
        "positive_feedback": handle_positive,
    },
)
compiled = graph.compile()

# ── Test emails ────────────────────────────────────────────────────

TEST_EMAILS = [
    "My order #12345 arrived completely smashed. The box was torn open "
    "and the product inside is broken. I want a full refund immediately.",

    "Hi, I was wondering what your return policy is? I bought a jacket "
    "last week but it doesn't fit. Can I exchange it for a different size?",

    "Just wanted to say thank you! The delivery was super fast and the "
    "quality is amazing. Will definitely order again!",
]


async def main():
    print("=" * 60)
    print(f"  AO Real-LLM Demo — Ollama ({OLLAMA_MODEL})")
    print("=" * 60)

    for i, email in enumerate(TEST_EMAILS, 1):
        print(f"\n{'─' * 60}")
        print(f"EMAIL {i}:")
        print(f"  {email[:80]}...")
        print(f"{'─' * 60}")

        # Run workflow
        result = await compiled.ainvoke({
            "input": email,
            "route": "",
            "messages": [],
            "output": "",
        })

        print(f"  Route:  {result['route']}")
        print(f"  Reply:")
        for line in result["output"].split("\n"):
            print(f"    {line}")

        # Post-execution policy check
        eval_result = await policy_engine.evaluate(
            PolicyStage.POST_EXECUTION,
            policies,
            {"output": result["output"]},
        )
        if not eval_result.allowed:
            print(f"  ⚠ POLICY BLOCKED: {[r.detail for r in eval_result.results if not r.passed]}")
        else:
            flagged = [r for r in eval_result.results if not r.passed]
            if flagged:
                print(f"  Policy warnings: {[r.detail for r in flagged]}")
            else:
                print(f"  Policy: ✓ all checks passed")

    print(f"\n{'=' * 60}")
    print("  Demo complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
