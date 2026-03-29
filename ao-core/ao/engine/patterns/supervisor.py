"""Supervisor multi-agent pattern — supervisor delegates to worker agents.

A supervisor node decides which worker to call next (or to finish).
Workers execute and return results to the supervisor, which decides
the next action. Loop continues until supervisor signals completion.

  START → supervisor → worker_A → supervisor → worker_B → supervisor → END
"""

from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph


class SupervisorState(TypedDict):
    """Default state for a supervisor workflow."""

    input: str
    messages: list[dict[str, str]]
    next_worker: str  # "worker_name" or "FINISH"
    output: str
    iterations: int


def build_supervisor(
    supervisor_fn: Callable[..., Any],
    workers: dict[str, Callable[..., Any]],
    max_iterations: int = 10,
    state_schema: type = SupervisorState,
) -> StateGraph:
    """Build a LangGraph state graph with a supervisor-worker loop.

    Args:
        supervisor_fn: Callable that inspects state and returns
                       {"next_worker": "<name>" or "FINISH"}.
        workers: Map of worker_name → callable.
        max_iterations: Max loops before forced termination.
        state_schema: TypedDict class for the graph state.

    Returns:
        A compiled-ready StateGraph.
    """
    graph = StateGraph(state_schema)

    # Add nodes
    graph.add_node("supervisor", supervisor_fn)
    for name, fn in workers.items():
        graph.add_node(name, fn)

    # Entry
    graph.set_entry_point("supervisor")

    # Supervisor decides next worker or finishes
    worker_names = list(workers.keys())

    def supervisor_router(state: dict) -> str:
        next_w = state.get("next_worker", "FINISH")
        iterations = state.get("iterations", 0)
        if iterations >= max_iterations:
            return "FINISH"
        if next_w in worker_names:
            return next_w
        return "FINISH"

    routing_map = {name: name for name in worker_names}
    routing_map["FINISH"] = END

    graph.add_conditional_edges("supervisor", supervisor_router, routing_map)

    # Each worker returns to supervisor
    for name in worker_names:
        graph.add_edge(name, "supervisor")

    return graph
