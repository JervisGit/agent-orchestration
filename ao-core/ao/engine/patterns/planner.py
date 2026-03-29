"""Plan-and-execute pattern — planner creates steps, executor runs them.

A planner node creates a list of steps. The executor runs them one by one.
After each step, a replan node decides whether to continue, replan, or finish.

  START → planner → executor → replan → executor → ... → END
"""

from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph


class PlannerState(TypedDict):
    """Default state for a plan-and-execute workflow."""

    input: str
    plan: list[str]                    # List of step descriptions
    current_step_index: int
    step_results: list[dict[str, Any]]
    messages: list[dict[str, str]]
    output: str
    status: str  # "planning", "executing", "replanning", "done"


def build_planner(
    plan_fn: Callable[..., Any],
    execute_fn: Callable[..., Any],
    replan_fn: Callable[..., Any] | None = None,
    state_schema: type = PlannerState,
) -> StateGraph:
    """Build a LangGraph state graph for plan-and-execute.

    Args:
        plan_fn: Creates the initial plan. Returns {"plan": [...], "status": "executing"}.
        execute_fn: Executes the current step. Returns updated step_results and increments index.
        replan_fn: Optional. Inspects progress and returns {"status": "executing"} to continue,
                   {"status": "planning"} to replan, or {"status": "done"} to finish.
                   If None, a default replan is used that finishes when all steps are done.
        state_schema: TypedDict class for the graph state.

    Returns:
        A compiled-ready StateGraph.
    """
    graph = StateGraph(state_schema)

    graph.add_node("planner", plan_fn)
    graph.add_node("executor", execute_fn)

    if replan_fn:
        graph.add_node("replan", replan_fn)
    else:
        graph.add_node("replan", _default_replan)

    # Entry
    graph.set_entry_point("planner")

    # planner → executor
    graph.add_edge("planner", "executor")

    # executor → replan
    graph.add_edge("executor", "replan")

    # replan → executor (continue) or END (done) or planner (replan)
    def replan_router(state: dict) -> str:
        status = state.get("status", "done")
        if status == "executing":
            return "executor"
        if status == "planning":
            return "planner"
        return "done"

    graph.add_conditional_edges("replan", replan_router, {
        "executor": "executor",
        "planner": "planner",
        "done": END,
    })

    return graph


def _default_replan(state: PlannerState) -> dict:
    """Default replan: continue if steps remain, else finish."""
    plan = state.get("plan", [])
    index = state.get("current_step_index", 0)
    if index < len(plan):
        return {"status": "executing"}
    return {"status": "done"}
