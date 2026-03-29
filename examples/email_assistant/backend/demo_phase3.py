"""Phase 3 Demo — Advanced Patterns & Platform Components.

Demonstrates:
1. Router pattern — classify and route to specialist agents
2. Supervisor pattern — supervisor delegates to workers iteratively
3. Planner pattern — plan-and-execute with replanning
4. Knowledge retrieval — in-memory knowledge source
5. Shared state — cross-agent communication within a workflow
6. Message bus — local dev async messaging
"""

import asyncio
import logging

from ao.engine.patterns.router import RouterState, build_router
from ao.engine.patterns.supervisor import SupervisorState, build_supervisor
from ao.engine.patterns.planner import PlannerState, build_planner
from ao.memory.knowledge import InMemoryKnowledgeSource, KnowledgeResult
from ao.memory.shared import MessageBus, SharedState

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")


# ── 1. Router Pattern ──────────────────────────────────────────────


def demo_router_classify(state: RouterState) -> dict:
    """Mock classifier: route based on keyword."""
    text = state["input"].lower()
    if "urgent" in text:
        return {"route": "escalation", "messages": [{"role": "router", "content": "→ escalation"}]}
    return {"route": "standard", "messages": [{"role": "router", "content": "→ standard"}]}


def demo_standard_handler(state: RouterState) -> dict:
    return {"output": f"[Standard] Handled: {state['input']}", "messages": [{"role": "standard", "content": "done"}]}


def demo_escalation_handler(state: RouterState) -> dict:
    return {"output": f"[ESCALATION] Priority handling: {state['input']}", "messages": [{"role": "escalation", "content": "done"}]}


async def run_router_demo():
    print("\n" + "=" * 60)
    print("DEMO 1: Router Pattern")
    print("=" * 60)

    graph = build_router(
        router_fn=demo_router_classify,
        specialists={"standard": demo_standard_handler, "escalation": demo_escalation_handler},
    )
    compiled = graph.compile()

    for text in ["Hello, I need help with my account", "URGENT: system is down!"]:
        result = await compiled.ainvoke({"input": text, "route": "", "messages": [], "output": ""})
        print(f"  Input:  {text}")
        print(f"  Output: {result['output']}")
        print(f"  Route:  {result['route']}")
        print()


# ── 2. Supervisor Pattern ──────────────────────────────────────────


def demo_supervisor(state: SupervisorState) -> dict:
    """Mock supervisor: delegates to research first, then writer, then FINISH."""
    iterations = state.get("iterations", 0)
    if iterations == 0:
        return {"next_worker": "researcher", "iterations": iterations + 1}
    if iterations == 1:
        return {"next_worker": "writer", "iterations": iterations + 1}
    return {"next_worker": "FINISH", "iterations": iterations + 1, "output": "Supervisor complete."}


def demo_researcher(state: SupervisorState) -> dict:
    return {"messages": [*state.get("messages", []), {"role": "researcher", "content": "Found 3 relevant docs"}]}


def demo_writer(state: SupervisorState) -> dict:
    return {"messages": [*state.get("messages", []), {"role": "writer", "content": "Draft written"}]}


async def run_supervisor_demo():
    print("\n" + "=" * 60)
    print("DEMO 2: Supervisor Pattern")
    print("=" * 60)

    graph = build_supervisor(
        supervisor_fn=demo_supervisor,
        workers={"researcher": demo_researcher, "writer": demo_writer},
        max_iterations=10,
    )
    compiled = graph.compile()

    result = await compiled.ainvoke({
        "input": "Write a summary of Q4 results",
        "messages": [],
        "next_worker": "",
        "output": "",
        "iterations": 0,
    })
    print(f"  Input:      {result['input']}")
    print(f"  Output:     {result['output']}")
    print(f"  Iterations: {result['iterations']}")
    print(f"  Messages:   {[m['content'] for m in result['messages']]}")
    print()


# ── 3. Planner Pattern ────────────────────────────────────────────


def demo_planner(state: PlannerState) -> dict:
    return {
        "plan": ["Gather data", "Analyze trends", "Generate report"],
        "current_step_index": 0,
        "step_results": [],
        "status": "executing",
    }


def demo_executor(state: PlannerState) -> dict:
    idx = state.get("current_step_index", 0)
    plan = state.get("plan", [])
    step = plan[idx] if idx < len(plan) else "unknown"
    results = list(state.get("step_results", []))
    results.append({"step": step, "result": f"Completed: {step}"})
    return {"step_results": results, "current_step_index": idx + 1}


async def run_planner_demo():
    print("\n" + "=" * 60)
    print("DEMO 3: Planner Pattern")
    print("=" * 60)

    graph = build_planner(plan_fn=demo_planner, execute_fn=demo_executor)
    compiled = graph.compile()

    result = await compiled.ainvoke({
        "input": "Generate quarterly report",
        "plan": [],
        "current_step_index": 0,
        "step_results": [],
        "messages": [],
        "output": "",
        "status": "planning",
    })
    print(f"  Input:   {result['input']}")
    print(f"  Plan:    {result['plan']}")
    print(f"  Status:  {result['status']}")
    print(f"  Results: {[r['result'] for r in result['step_results']]}")
    print()


# ── 4. Knowledge Retrieval ─────────────────────────────────────────


async def run_knowledge_demo():
    print("\n" + "=" * 60)
    print("DEMO 4: Knowledge Retrieval (In-Memory)")
    print("=" * 60)

    kb = InMemoryKnowledgeSource()
    kb.add_document("The refund policy allows returns within 30 days.", {"category": "policy"})
    kb.add_document("Premium support is available 24/7 for enterprise.", {"category": "support"})
    kb.add_document("Password reset can be done via the settings page.", {"category": "account"})

    for query in ["refund", "password", "shipping"]:
        results: list[KnowledgeResult] = await kb.search(query)
        print(f"  Query: '{query}' → {len(results)} result(s)")
        for r in results:
            print(f"    - {r.content[:60]}...")
    print()


# ── 5. Shared State & Message Bus ─────────────────────────────────


async def run_shared_state_demo():
    print("\n" + "=" * 60)
    print("DEMO 5: Shared State & Message Bus")
    print("=" * 60)

    # Shared state
    shared = SharedState()
    shared.set("wf-001", "user_intent", "complaint")
    shared.set("wf-001", "sentiment", "negative")
    print(f"  SharedState['wf-001']['user_intent'] = {shared.get('wf-001', 'user_intent')}")
    print(f"  SharedState['wf-001']['sentiment']   = {shared.get('wf-001', 'sentiment')}")

    # Message bus (local dev mode — no Azure Service Bus)
    bus = MessageBus()
    await bus.publish("email.classified", {"class": "complaint"}, sender_workflow_id="wf-001")
    await bus.publish("email.classified", {"class": "inquiry"}, sender_workflow_id="wf-002")
    msgs = await bus.consume_local("email.classified")
    print(f"  MessageBus: consumed {len(msgs)} message(s) from 'email.classified'")
    for m in msgs:
        print(f"    - from {m['sender_workflow_id']}: {m['payload']}")
    print()


# ── Main ───────────────────────────────────────────────────────────


async def main():
    print("=" * 60)
    print("  AO Phase 3 — Advanced Patterns & Platform Components")
    print("=" * 60)

    await run_router_demo()
    await run_supervisor_demo()
    await run_planner_demo()
    await run_knowledge_demo()
    await run_shared_state_demo()

    print("=" * 60)
    print("  All Phase 3 demos completed successfully!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
