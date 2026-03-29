"""Linear chain pattern — sequential step execution.

Each step is a callable (sync or async) that receives the state dict
and returns an updated state dict. Steps execute in order.
"""

from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph


class LinearState(TypedDict):
    """Default state for a linear chain."""

    input: str
    messages: list[dict[str, str]]
    output: str
    steps_completed: list[str]


def build_linear_chain(
    steps: list[tuple[str, Callable[..., Any]]],
    state_schema: type = LinearState,
) -> StateGraph:
    """Build a LangGraph state graph that runs steps sequentially.

    Args:
        steps: List of (step_name, callable) pairs. Each callable
               takes a state dict and returns a partial state update.
        state_schema: TypedDict class for the graph state.

    Returns:
        A compiled-ready StateGraph.
    """
    graph = StateGraph(state_schema)

    for name, fn in steps:
        graph.add_node(name, fn)

    # Wire sequentially: START -> step1 -> step2 -> ... -> END
    if steps:
        graph.set_entry_point(steps[0][0])
        for i in range(len(steps) - 1):
            graph.add_edge(steps[i][0], steps[i + 1][0])
        graph.add_edge(steps[-1][0], END)

    return graph