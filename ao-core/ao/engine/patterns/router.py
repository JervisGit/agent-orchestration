"""Router pattern — route to specialized agents based on input.

A classifier node inspects the input and routes to one of N specialist
nodes. Each specialist handles its domain, then control flows to a
shared output node.

  START → router → specialist_A → output → END
                 → specialist_B → output → END
                 → specialist_C → output → END
"""

from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph


class RouterState(TypedDict):
    """Default state for a router workflow."""

    input: str
    route: str
    messages: list[dict[str, str]]
    output: str


def build_router(
    router_fn: Callable[..., Any],
    specialists: dict[str, Callable[..., Any]],
    output_fn: Callable[..., Any] | None = None,
    state_schema: type = RouterState,
) -> StateGraph:
    """Build a LangGraph state graph that routes input to a specialist.

    Args:
        router_fn: Callable that receives state and returns a state update
                   that must include {"route": "<specialist_name>"}.
        specialists: Map of route_name → callable for each specialist.
        output_fn: Optional final node after the specialist runs.
        state_schema: TypedDict class for the graph state.

    Returns:
        A compiled-ready StateGraph.
    """
    graph = StateGraph(state_schema)

    # Add nodes
    graph.add_node("router", router_fn)
    for name, fn in specialists.items():
        graph.add_node(name, fn)
    if output_fn:
        graph.add_node("output", output_fn)

    # Entry
    graph.set_entry_point("router")

    # Conditional edge from router → specialist based on 'route' field
    def route_selector(state: dict) -> str:
        return state.get("route", list(specialists.keys())[0])

    graph.add_conditional_edges("router", route_selector, {
        name: name for name in specialists
    })

    # Each specialist → output (or END)
    for name in specialists:
        if output_fn:
            graph.add_edge(name, "output")
        else:
            graph.add_edge(name, END)

    if output_fn:
        graph.add_edge("output", END)

    return graph
