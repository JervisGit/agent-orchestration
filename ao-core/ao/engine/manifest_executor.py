"""ManifestExecutor — builds and runs agentic workflows from an AppManifest.

App teams declare agents, SOPs, and policies in ao-manifest.yaml.
ManifestExecutor handles all LangGraph wiring so app code never imports
StateGraph, END, or any other LangGraph primitive.

Usage
-----
    from ao.engine.manifest_executor import ManifestExecutor
    from ao.config.manifest import AppManifest

    manifest = AppManifest.from_yaml("ao-manifest.yaml")
    executor = ManifestExecutor(manifest, llm=llm, langfuse_client=lf)
    executor.register_pre_step("lookup_taxpayer", node_lookup_taxpayer)
    compiled_graph = executor.compile(state_schema=MyAppState)

    # SSE streaming (same API as compiled_graph.astream):
    async for chunk in executor.astream(state, stream_mode="updates"):
        ...

    # Batch:
    result = await executor.ainvoke(state)

Convention: _context state key
-------------------------------
Pre-steps that want to inject contextual text into specialist system prompts
should set state["_context"] to a plain string. The executor prepends this
to every specialist agent's system prompt automatically. This decouples the
data schema (e.g. taxpayer record format) from the executor.

Tracing
-------
If langfuse_client is provided, the executor opens a Langfuse trace per
astream()/ainvoke() call, attaches a Generation for the classifier and each
specialist agent, and closes the trace when the run finishes.

App-specific pre-steps (e.g. DB lookup node) can retrieve the live trace
via executor.get_trace(trace_id) and attach custom spans to it.
"""

import logging
import uuid
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

from langgraph.graph import END, StateGraph

from ao.config.manifest import AgentConfig, AppManifest
from ao.engine.patterns.router import RouterState
from ao.llm.base import LLMProvider

logger = logging.getLogger(__name__)

# Restricted eval namespace for hitl_condition expressions
_EVAL_BUILTINS: dict[str, Any] = {"True": True, "False": False, "None": None}


class ManifestExecutor:
    """Builds a LangGraph workflow from an AppManifest and manages its lifecycle."""

    def __init__(
        self,
        manifest: AppManifest,
        llm: LLMProvider,
        langfuse_client: Any | None = None,
    ) -> None:
        self._manifest = manifest
        self._llm = llm
        self._langfuse = langfuse_client
        self._pre_steps: list[tuple[str, Callable]] = []
        self._compiled: Any = None
        self._active_traces: dict[str, Any] = {}

    # ── Registration ────────────────────────────────────────────────

    def register_pre_step(self, name: str, fn: Callable) -> "ManifestExecutor":
        """Register a node to execute before classification (e.g. DB lookup).

        Steps are executed in registration order, then the classifier runs.
        Returns self for fluent chaining.
        """
        self._pre_steps.append((name, fn))
        return self

    # ── Compile ─────────────────────────────────────────────────────

    def compile(self, state_schema: type = RouterState) -> Any:
        """Build and compile the LangGraph from the manifest.

        Returns the compiled graph (same object returned by StateGraph.compile()).
        Also stored as self._compiled for use by astream()/ainvoke().
        """
        if self._manifest.pattern != "router":
            raise NotImplementedError(
                f"Pattern '{self._manifest.pattern}' not yet supported by ManifestExecutor. "
                "Use 'router' or register your graph manually via LangGraphEngine."
            )

        agents = {a.name: a for a in self._manifest.agents}
        classifier_name = self._manifest.classifier_agent

        if classifier_name not in agents:
            raise ValueError(
                f"classifier_agent '{classifier_name}' not found in manifest agents: "
                f"{list(agents.keys())}"
            )

        classifier_cfg = agents[classifier_name]
        specialists_cfg = {k: v for k, v in agents.items() if k != classifier_name}
        categories = list(specialists_cfg.keys())

        graph = StateGraph(state_schema)

        # ── Pre-steps ──────────────────────────────────────────────
        prev: str | None = None
        for step_name, step_fn in self._pre_steps:
            graph.add_node(step_name, step_fn)
            if prev is None:
                graph.set_entry_point(step_name)
            else:
                graph.add_edge(prev, step_name)
            prev = step_name

        # ── Classifier ─────────────────────────────────────────────
        graph.add_node("classify", self._make_classifier_node(classifier_cfg, categories))
        if prev:
            graph.add_edge(prev, "classify")
        else:
            graph.set_entry_point("classify")

        # ── Specialists ────────────────────────────────────────────
        for name, cfg in specialists_cfg.items():
            graph.add_node(name, self._make_specialist_node(cfg, categories))
            graph.add_edge(name, END)

        graph.add_conditional_edges(
            "classify",
            lambda s: s.get("route", categories[-1]),
            {name: name for name in specialists_cfg},
        )

        self._compiled = graph.compile()
        logger.info(
            "ManifestExecutor compiled '%s' (%s pattern, %d agents, %d pre-steps)",
            self._manifest.app_id, self._manifest.pattern,
            len(self._manifest.agents), len(self._pre_steps),
        )
        return self._compiled

    # ── Streaming / invocation ──────────────────────────────────────

    async def astream(self, state: dict, **kwargs) -> AsyncGenerator:
        """Stream workflow updates. Opens/closes a Langfuse trace around the run.

        Yields the same chunks as compiled_graph.astream().
        """
        if self._compiled is None:
            raise RuntimeError("Call compile() before astream().")

        trace_id: str = state.get("trace_id") or str(uuid.uuid4())
        lf_trace = self._open_trace(trace_id, state)

        try:
            async for chunk in self._compiled.astream(state, **kwargs):
                yield chunk
        finally:
            self._close_trace(trace_id, state)

    async def ainvoke(self, state: dict, **kwargs) -> dict:
        """Invoke workflow and return final state. Manages Langfuse trace lifecycle."""
        if self._compiled is None:
            raise RuntimeError("Call compile() before ainvoke().")

        trace_id: str = state.get("trace_id") or str(uuid.uuid4())
        self._open_trace(trace_id, state)

        try:
            result = await self._compiled.ainvoke(state, **kwargs)
            return result
        finally:
            self._close_trace(trace_id, result if "result" in dir() else state)

    # ── Trace helpers (called by pre-steps for custom spans) ────────

    def get_trace(self, trace_id: str) -> Any | None:
        """Return the active Langfuse trace for trace_id, or None if not tracing."""
        return self._active_traces.get(trace_id)

    # ── Internal ────────────────────────────────────────────────────

    def _open_trace(self, trace_id: str, state: dict) -> Any | None:
        lf_trace = None
        if self._langfuse:
            try:
                lf_trace = self._langfuse.trace(
                    name=f"workflow:{self._manifest.app_id}",
                    id=trace_id,
                    input=state.get("input", ""),
                    metadata={
                        "app_id": self._manifest.app_id,
                        "email_id": state.get("email_id"),
                        "sender": state.get("sender"),
                    },
                )
            except Exception:
                logger.warning("Langfuse trace() failed — continuing without tracing", exc_info=True)
        self._active_traces[trace_id] = lf_trace
        return lf_trace

    def _close_trace(self, trace_id: str, final_state: dict) -> None:
        lf_trace = self._active_traces.pop(trace_id, None)
        if lf_trace:
            try:
                lf_trace.update(
                    output=final_state.get("output", ""),
                    metadata={
                        "category": final_state.get("route"),
                        "hitl": final_state.get("hitl_required", False),
                        "policy_flags": final_state.get("policy_flags", []),
                    },
                )
            except Exception:
                logger.warning("Langfuse trace.update() failed", exc_info=True)

    def _make_classifier_node(self, cfg: AgentConfig, categories: list[str]) -> Callable:
        """Return an async node function that classifies input into one of the categories."""
        llm = self._llm
        executor = self  # closure reference for tracing

        categories_text = "\n".join(f"  {c}" for c in categories)

        async def node_classify(state: dict) -> dict:
            system_prompt = cfg.system_prompt.replace("{categories}", categories_text)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": state["input"]},
            ]

            lf_trace = executor.get_trace(state.get("trace_id", ""))
            lf_gen = None
            if lf_trace:
                try:
                    lf_gen = lf_trace.generation(
                        name="classify",
                        model=getattr(llm, "default_model", cfg.model),
                        input=messages,
                        metadata={"categories": categories, **cfg.trace_metadata},
                    )
                except Exception:
                    pass

            resp = await llm.complete(messages=messages, temperature=cfg.temperature)
            raw = resp.content.strip().lower().replace(" ", "_")
            route = raw if raw in categories else categories[-1]

            if lf_gen:
                try:
                    lf_gen.end(
                        output=route,
                        usage={
                            "input": resp.usage.get("input_tokens", 0),
                            "output": resp.usage.get("output_tokens", 0),
                        },
                    )
                except Exception:
                    pass

            return {
                "route": route,
                "messages": state.get("messages", []) + [{"role": "classifier", "content": route}],
            }

        return node_classify

    def _make_specialist_node(self, cfg: AgentConfig, categories: list[str]) -> Callable:
        """Return an async node function for a specialist agent."""
        llm = self._llm
        executor = self

        async def node_specialist(state: dict) -> dict:
            # Build system prompt: optional context prefix + base prompt + SOP
            parts: list[str] = []
            ctx = state.get("_context", "")
            if ctx:
                parts.append(ctx)
            parts.append(cfg.system_prompt)
            if cfg.sop:
                parts.append(f"SOP YOU MUST FOLLOW:\n{cfg.sop}")
            system_prompt = "\n\n".join(parts)

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": state["input"]},
            ]

            lf_trace = executor.get_trace(state.get("trace_id", ""))
            lf_gen = None
            if lf_trace:
                try:
                    lf_gen = lf_trace.generation(
                        name=f"specialist-{cfg.name}",
                        model=getattr(llm, "default_model", cfg.model),
                        input=messages,
                        metadata={"category": cfg.name, "sop_applied": bool(cfg.sop), **cfg.trace_metadata},
                    )
                except Exception:
                    pass

            resp = await llm.complete(messages=messages, temperature=cfg.temperature)

            if lf_gen:
                try:
                    lf_gen.end(
                        output=resp.content,
                        usage={
                            "input": resp.usage.get("input_tokens", 0),
                            "output": resp.usage.get("output_tokens", 0),
                        },
                    )
                except Exception:
                    pass

            result: dict = {
                "output": resp.content,
                "messages": state.get("messages", []) + [{"role": "agent", "content": resp.content}],
            }

            # Evaluate HITL condition from manifest
            if cfg.hitl_condition:
                try:
                    tp = state.get("taxpayer")
                    hitl = bool(eval(  # noqa: S307 — developer-authored manifest config only
                        cfg.hitl_condition,
                        {"__builtins__": _EVAL_BUILTINS},
                        {"state": state, "taxpayer": tp, "output": resp.content},
                    ))
                    if hitl:
                        flags = list(state.get("policy_flags", []))
                        flags.append(
                            f"HITL_REQUIRED: agent={cfg.name} condition=({cfg.hitl_condition})"
                        )
                        result["hitl_required"] = True
                        result["policy_flags"] = flags
                        logger.info(
                            "HITL triggered for agent '%s' (trace %s)",
                            cfg.name, state.get("trace_id", "?"),
                        )
                except Exception:
                    logger.warning("hitl_condition eval failed for agent '%s'", cfg.name, exc_info=True)

            return result

        return node_specialist

    # ── Convenience constructor ─────────────────────────────────────

    @classmethod
    def from_yaml(
        cls,
        path: str | Path,
        llm: LLMProvider,
        langfuse_client: Any | None = None,
    ) -> "ManifestExecutor":
        """Create a ManifestExecutor directly from a YAML manifest file path."""
        manifest = AppManifest.from_yaml(path)
        return cls(manifest, llm=llm, langfuse_client=langfuse_client)
