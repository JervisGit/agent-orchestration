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

import asyncio
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
        if self._manifest.pattern == "router":
            return self._compile_router(state_schema)
        if self._manifest.pattern in ("concurrent", "magentic"):
            return self._compile_concurrent(state_schema)
        raise NotImplementedError(
            f"Pattern '{self._manifest.pattern}' not yet supported by ManifestExecutor. "
            "Supported: 'router', 'concurrent'. 'magentic' is an alias for 'concurrent'. "
            "For 'linear', 'supervisor', 'planner', register your graph via LangGraphEngine."
        )

    def _compile_router(self, state_schema: type) -> Any:
        """Build the router pattern: [pre-steps] → classify → specialist → END."""
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
            "ManifestExecutor compiled '%s' (router pattern, %d agents, %d pre-steps)",
            self._manifest.app_id, len(self._manifest.agents), len(self._pre_steps),
        )
        return self._compiled

    def _compile_concurrent(self, state_schema: type) -> Any:
        """Build the concurrent pattern: [pre-steps] → intent_classify → dispatch → merge → END.

        Also accessible as pattern='magentic' (alias kept for backwards compatibility).
        Corresponds to Microsoft's 'Concurrent orchestration' pattern.
        """
        agents = {a.name: a for a in self._manifest.agents}
        classifier_name = self._manifest.classifier_agent

        if classifier_name not in agents:
            raise ValueError(
                f"classifier_agent '{classifier_name}' not found in manifest agents: "
                f"{list(agents.keys())}"
            )

        classifier_cfg = agents[classifier_name]

        # Eligible specialists: explicit intent_agents list, or all non-classifier agents
        intent_names = self._manifest.intent_agents or [
            k for k in agents if k != classifier_name
        ]
        specialists_cfg = {k: agents[k] for k in intent_names if k in agents}
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

        # ── Intent classifier ──────────────────────────────────────
        graph.add_node("intent_classify", self._make_intent_classifier_node(classifier_cfg, categories))
        if prev:
            graph.add_edge(prev, "intent_classify")
        else:
            graph.set_entry_point("intent_classify")

        # ── Dispatch (parallel specialist execution) ───────────────
        graph.add_node("dispatch", self._make_dispatch_node(specialists_cfg))
        graph.add_edge("intent_classify", "dispatch")

        # ── Merge ──────────────────────────────────────────────────
        graph.add_node("merge", self._make_merge_node())
        graph.add_edge("dispatch", "merge")
        graph.add_edge("merge", END)

        self._compiled = graph.compile()
        logger.info(
            "ManifestExecutor compiled '%s' (concurrent pattern, %d intent agents, %d pre-steps)",
            self._manifest.app_id, len(specialists_cfg), len(self._pre_steps),
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
                # router uses 'route'; magentic uses 'intents' — include whichever is present
                category = final_state.get("route") or final_state.get("intents")
                lf_trace.update(
                    output=final_state.get("output", ""),
                    metadata={
                        "category": category,
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
                        result["hitl_action"] = cfg.hitl_action or f"Review {cfg.name} decision"
                        result["policy_flags"] = flags
                        logger.info(
                            "HITL triggered for agent '%s' (trace %s)",
                            cfg.name, state.get("trace_id", "?"),
                        )
                except Exception:
                    logger.warning("hitl_condition eval failed for agent '%s'", cfg.name, exc_info=True)

            return result

        return node_specialist

    def _make_intent_classifier_node(self, cfg: AgentConfig, categories: list[str]) -> Callable:
        """Return an async node that detects ALL matching intents (magentic pattern).

        The system prompt should instruct the LLM to return a comma-separated list.
        Falls back to [categories[-1]] when no valid intent is detected.
        """
        llm = self._llm
        executor = self

        categories_text = "\n".join(f"  {c}" for c in categories)

        async def node_intent_classify(state: dict) -> dict:
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
                        name="intent_classify",
                        model=getattr(llm, "default_model", cfg.model),
                        input=messages,
                        metadata={"categories": categories, **cfg.trace_metadata},
                    )
                except Exception:
                    pass

            resp = await llm.complete(messages=messages, temperature=cfg.temperature)

            # Parse comma-separated list; keep only known categories
            raw_parts = [p.strip().lower().replace(" ", "_") for p in resp.content.split(",")]
            intents = [p for p in raw_parts if p in categories]
            if not intents:
                intents = [categories[-1]]

            if lf_gen:
                try:
                    lf_gen.end(
                        output=intents,
                        usage={
                            "input": resp.usage.get("input_tokens", 0),
                            "output": resp.usage.get("output_tokens", 0),
                        },
                    )
                except Exception:
                    pass

            return {
                "intents": intents,
                "messages": state.get("messages", []) + [
                    {"role": "intent_classifier", "content": ", ".join(intents)}
                ],
            }

        return node_intent_classify

    def _make_dispatch_node(self, specialists_cfg: dict[str, AgentConfig]) -> Callable:
        """Return an async node that runs all detected specialists concurrently.

        Uses asyncio.gather for true parallel execution. Merges HITL flags and
        policy_flags from all specialists. Returns specialist_outputs dict.
        """
        executor = self

        async def _run_one(state: dict, cfg: AgentConfig) -> dict:
            """Run a single specialist and return its result dict."""
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
                        model=getattr(executor._llm, "default_model", cfg.model),
                        input=messages,
                        metadata={
                            "category": cfg.name,
                            "sop_applied": bool(cfg.sop),
                            "pattern": "magentic",
                            **cfg.trace_metadata,
                        },
                    )
                except Exception:
                    pass

            resp = await executor._llm.complete(messages=messages, temperature=cfg.temperature)

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

            result: dict = {"output": resp.content, "hitl_required": False, "policy_flags": [], "hitl_action": ""}

            if cfg.hitl_condition:
                try:
                    tp = state.get("taxpayer")
                    hitl = bool(eval(  # noqa: S307 — developer-authored manifest config only
                        cfg.hitl_condition,
                        {"__builtins__": _EVAL_BUILTINS},
                        {"state": state, "taxpayer": tp, "output": resp.content},
                    ))
                    if hitl:
                        result["hitl_required"] = True
                        result["hitl_action"] = cfg.hitl_action or f"Review {cfg.name} decision"
                        result["policy_flags"].append(
                            f"HITL_REQUIRED: agent={cfg.name} condition=({cfg.hitl_condition})"
                        )
                        logger.info(
                            "HITL triggered for agent '%s' (trace %s)",
                            cfg.name, state.get("trace_id", "?"),
                        )
                except Exception:
                    logger.warning(
                        "hitl_condition eval failed for agent '%s'", cfg.name, exc_info=True
                    )

            return result

        async def node_dispatch(state: dict) -> dict:
            intents = state.get("intents", [])
            valid = [i for i in intents if i in specialists_cfg]
            if not valid:
                valid = [list(specialists_cfg.keys())[-1]]

            results = await asyncio.gather(*[_run_one(state, specialists_cfg[name]) for name in valid])

            specialist_outputs = {name: r["output"] for name, r in zip(valid, results)}
            hitl_required = any(r["hitl_required"] for r in results)
            hitl_action = next((r["hitl_action"] for r in results if r.get("hitl_action")), "")

            all_flags = list(state.get("policy_flags", []))
            for r in results:
                all_flags.extend(r["policy_flags"])

            return {
                "specialist_outputs": specialist_outputs,
                "intents": valid,
                "hitl_required": hitl_required,
                "hitl_action": hitl_action,
                "policy_flags": all_flags,
                "messages": state.get("messages", []) + [
                    {"role": "dispatch", "content": f"Dispatched to: {', '.join(valid)}"}
                ],
            }

        return node_dispatch

    def _make_merge_node(self) -> Callable:
        """Return an async node that merges specialist outputs into a single reply.

        For a single intent, returns that specialist's output directly.
        For multiple intents, calls the LLM to synthesise a unified reply.
        """
        llm = self._llm

        async def node_merge(state: dict) -> dict:
            specialist_outputs: dict = state.get("specialist_outputs", {})

            if len(specialist_outputs) == 0:
                output = state.get("output", "")
            elif len(specialist_outputs) == 1:
                output = next(iter(specialist_outputs.values()))
            else:
                sections = "\n\n".join(
                    f"--- {name.replace('_', ' ').title()} Specialist Response ---\n{text}"
                    for name, text in specialist_outputs.items()
                )
                merge_messages = [
                    {
                        "role": "system",
                        "content": (
                            "You are a senior tax authority officer finalising a reply email. "
                            "You have received specialist responses for multiple parts of one "
                            "taxpayer enquiry. Combine them into a single, coherent, professional "
                            "reply email. Do not repeat information. Address each point clearly. "
                            "Keep the total reply under 350 words."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Original email:\n{state.get('input', '')}\n\n"
                            f"Specialist responses to combine:\n{sections}"
                        ),
                    },
                ]
                resp = await llm.complete(messages=merge_messages, temperature=0.1)
                output = resp.content

            return {
                "output": output,
                "messages": state.get("messages", []) + [{"role": "merge", "content": output}],
            }

        return node_merge

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
