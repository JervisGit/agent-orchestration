"""Magentic orchestration pattern — multi-intent parallel specialist dispatch.

An intent-classifier node detects ALL applicable intents from the input.
A dispatcher node runs each matched specialist concurrently via asyncio.gather.
A merge node synthesises the individual specialist outputs into a single reply.

  START → [pre-steps] → intent_classify → dispatch → merge → END

Use when a single user message may span multiple domains that require
independent expert handling (e.g. "I need a filing extension AND a payment
plan" requires two specialists running in parallel, not one routing branch).

Contrast with 'router': router calls ONE specialist. Magentic calls N ≥ 1
specialists concurrently and merges results. With a single intent detected,
magentic degenerates to single-specialist execution with no merge overhead.
"""

from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph


class MagenticState(TypedDict):
    """Default state for a magentic workflow."""

    input: str
    intents: list[str]          # intent names detected by the classifier
    specialist_outputs: dict    # {intent_name: response_text}
    messages: list[dict[str, str]]
    output: str                 # final merged reply


def build_magentic(
    intent_classifier_fn: Callable[..., Any],
    dispatch_fn: Callable[..., Any],
    merge_fn: Callable[..., Any],
    state_schema: type = MagenticState,
) -> StateGraph:
    """Build a LangGraph for magentic multi-intent orchestration.

    Args:
        intent_classifier_fn: Callable that reads state["input"] and returns
            {"intents": ["intent_a", "intent_b", ...]}. The prompt should ask
            the LLM to return a comma-separated list of matching categories.
        dispatch_fn: Callable that reads state["intents"] and runs each matched
            specialist concurrently. Returns:
            {"specialist_outputs": {name: text}, "messages": [...],
             "hitl_required": bool, "policy_flags": [...]}.
        merge_fn: Callable that reads state["specialist_outputs"] and combines
            them into {"output": "...single coherent reply..."}.
        state_schema: TypedDict class for the graph state. Must contain
            'input', 'intents', 'specialist_outputs', 'messages', 'output'.

    Returns:
        A compiled-ready StateGraph (call .compile() before running).
    """
    graph = StateGraph(state_schema)

    graph.add_node("intent_classify", intent_classifier_fn)
    graph.add_node("dispatch", dispatch_fn)
    graph.add_node("merge", merge_fn)

    graph.set_entry_point("intent_classify")
    graph.add_edge("intent_classify", "dispatch")
    graph.add_edge("dispatch", "merge")
    graph.add_edge("merge", END)

    return graph
