"""Unit tests for orchestration patterns — router, supervisor, planner."""

import asyncio

import pytest

from ao.engine.patterns.linear import LinearState, build_linear_chain
from ao.engine.patterns.planner import PlannerState, build_planner
from ao.engine.patterns.router import RouterState, build_router
from ao.engine.patterns.supervisor import SupervisorState, build_supervisor


# ── Linear Chain ───────────────────────────────────────────────────


class TestLinearChain:
    def test_linear_chain_runs_steps_in_order(self):
        log = []

        def step_a(state: LinearState) -> dict:
            log.append("a")
            return {"messages": [*state.get("messages", []), {"role": "a", "content": "done"}]}

        def step_b(state: LinearState) -> dict:
            log.append("b")
            return {"output": "final", "messages": [*state.get("messages", []), {"role": "b", "content": "done"}]}

        graph = build_linear_chain([("step_a", step_a), ("step_b", step_b)])
        compiled = graph.compile()
        result = asyncio.get_event_loop().run_until_complete(
            compiled.ainvoke({"input": "test", "messages": [], "output": ""})
        )
        assert log == ["a", "b"]
        assert result["output"] == "final"


# ── Router ─────────────────────────────────────────────────────────


class TestRouter:
    def test_routes_to_correct_specialist(self):
        def classifier(state):
            return {"route": "billing" if "bill" in state["input"] else "support"}

        def billing(state):
            return {"output": "billing handled"}

        def support(state):
            return {"output": "support handled"}

        graph = build_router(
            router_fn=classifier,
            specialists={"billing": billing, "support": support},
        )
        compiled = graph.compile()

        r1 = asyncio.get_event_loop().run_until_complete(
            compiled.ainvoke({"input": "bill question", "route": "", "messages": [], "output": ""})
        )
        assert r1["output"] == "billing handled"
        assert r1["route"] == "billing"

        r2 = asyncio.get_event_loop().run_until_complete(
            compiled.ainvoke({"input": "help me", "route": "", "messages": [], "output": ""})
        )
        assert r2["output"] == "support handled"

    def test_output_node(self):
        def classifier(state):
            return {"route": "a"}

        def agent_a(state):
            return {"output": "from_a"}

        def output_fn(state):
            return {"output": f"[final] {state['output']}"}

        graph = build_router(
            router_fn=classifier,
            specialists={"a": agent_a},
            output_fn=output_fn,
        )
        compiled = graph.compile()
        result = asyncio.get_event_loop().run_until_complete(
            compiled.ainvoke({"input": "x", "route": "", "messages": [], "output": ""})
        )
        assert result["output"] == "[final] from_a"


# ── Supervisor ─────────────────────────────────────────────────────


class TestSupervisor:
    def test_supervisor_delegates_and_finishes(self):
        call_order = []

        def supervisor(state):
            i = state.get("iterations", 0)
            call_order.append(f"sup-{i}")
            if i == 0:
                return {"next_worker": "worker_a", "iterations": i + 1}
            return {"next_worker": "FINISH", "iterations": i + 1, "output": "done"}

        def worker_a(state):
            call_order.append("worker_a")
            return {"messages": [*state.get("messages", []), {"role": "a", "content": "ok"}]}

        graph = build_supervisor(
            supervisor_fn=supervisor, workers={"worker_a": worker_a}
        )
        compiled = graph.compile()
        result = asyncio.get_event_loop().run_until_complete(
            compiled.ainvoke(
                {"input": "task", "messages": [], "next_worker": "", "output": "", "iterations": 0}
            )
        )
        assert result["output"] == "done"
        assert call_order == ["sup-0", "worker_a", "sup-1"]

    def test_max_iterations_guard(self):
        def supervisor(state):
            i = state.get("iterations", 0)
            return {"next_worker": "w", "iterations": i + 1}

        def worker(state):
            return {}

        graph = build_supervisor(
            supervisor_fn=supervisor, workers={"w": worker}, max_iterations=2
        )
        compiled = graph.compile()
        result = asyncio.get_event_loop().run_until_complete(
            compiled.ainvoke(
                {"input": "loop", "messages": [], "next_worker": "", "output": "", "iterations": 0}
            )
        )
        # Should terminate at max_iterations
        assert result["iterations"] <= 3  # 2 real + 1 final supervisor call


# ── Planner ────────────────────────────────────────────────────────


class TestPlanner:
    def test_plan_and_execute_all_steps(self):
        def plan_fn(state):
            return {
                "plan": ["step1", "step2"],
                "current_step_index": 0,
                "step_results": [],
                "status": "executing",
            }

        def execute_fn(state):
            idx = state.get("current_step_index", 0)
            results = list(state.get("step_results", []))
            results.append({"step": idx, "done": True})
            return {"step_results": results, "current_step_index": idx + 1}

        graph = build_planner(plan_fn=plan_fn, execute_fn=execute_fn)
        compiled = graph.compile()
        result = asyncio.get_event_loop().run_until_complete(
            compiled.ainvoke({
                "input": "goal",
                "plan": [],
                "current_step_index": 0,
                "step_results": [],
                "messages": [],
                "output": "",
                "status": "planning",
            })
        )
        assert result["status"] == "done"
        assert len(result["step_results"]) == 2

    def test_custom_replan(self):
        def plan_fn(state):
            return {"plan": ["a"], "current_step_index": 0, "step_results": [], "status": "executing"}

        def execute_fn(state):
            idx = state.get("current_step_index", 0)
            results = list(state.get("step_results", []))
            results.append({"step": idx})
            return {"step_results": results, "current_step_index": idx + 1}

        def replan_fn(state):
            # After executing "a", add "b" and continue once, then finish
            if len(state.get("step_results", [])) == 1:
                return {"plan": ["a", "b"], "status": "executing"}
            return {"status": "done"}

        graph = build_planner(plan_fn=plan_fn, execute_fn=execute_fn, replan_fn=replan_fn)
        compiled = graph.compile()
        result = asyncio.get_event_loop().run_until_complete(
            compiled.ainvoke({
                "input": "x",
                "plan": [],
                "current_step_index": 0,
                "step_results": [],
                "messages": [],
                "output": "",
                "status": "planning",
            })
        )
        assert result["status"] == "done"
        assert len(result["step_results"]) == 2
