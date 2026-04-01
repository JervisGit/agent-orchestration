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

    # Register a callable tool the LLM can invoke (optional)
    executor.register_tool("lookup_taxpayer", fn, schema={...})

    # OR register a fixed pre-step (runs unconditionally before classification)
    executor.register_pre_step("lookup_taxpayer", node_lookup_taxpayer)

    compiled_graph = executor.compile(state_schema=MyAppState)

    # SSE streaming with token-level events:
    queue = asyncio.Queue()
    executor.set_token_stream(trace_id, queue)
    async for chunk in executor.astream(state, stream_mode="updates"):
        ...

    # Batch:
    result = await executor.ainvoke(state)

Tool calling
------------
When tools are registered, specialist agents receive the tool definitions via
the OpenAI `tools` parameter.  If the LLM calls a tool, ManifestExecutor
executes it, appends the result to the message history, and re-queries the LLM
for the final reply.  Each tool call is traced as a Langfuse span.

Token streaming
---------------
Call executor.set_token_stream(trace_id, queue) before astream().  The
executor will push {"node": name, "token": str} dicts to the queue as tokens
arrive from the LLM.  A sentinel {"node": name, "done": True} is pushed when a
node completes.  The SSE generator reads from both astream() (for node
boundaries) and the queue (for individual tokens).

Supervisor pattern
------------------
When pattern="supervisor" is set in the manifest, a supervisor agent
(the first agent with role="supervisor", or the first agent overall) reads the
request, decides which specialist to invoke next, and loops until it outputs
"FINISH".  Each specialist's output is accumulated in specialist_outputs and
surfaced to the supervisor on the next iteration.

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
import json as _json
import logging
import re as _re
import uuid
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from ao.config.manifest import AgentConfig, AppManifest
from ao.engine.patterns.router import RouterState
from ao.llm.base import LLMProvider
from ao.tools.schema import AgentMessage, ToolResult, ToolSchema

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
        # name -> (callable, ToolSchema)
        self._tools: dict[str, tuple[Callable, ToolSchema]] = {}
        # trace_id -> asyncio.Queue for token streaming
        self._token_queues: dict[str, asyncio.Queue] = {}
        # trace_id -> asyncio.Event; set to request cancellation mid-stream
        self._cancel_events: dict[str, asyncio.Event] = {}
        # Checkpointer — saves node-level state after every node so a cancelled run
        # can resume from the last completed node.
        # Uses Redis (AsyncRedisSaver) when REDIS_URL is set so checkpoints survive
        # container restarts.  Falls back to in-process MemorySaver for local dev.
        import os as _os
        _redis_url = _os.environ.get("REDIS_URL")
        if _redis_url:
            try:
                from langgraph.checkpoint.redis.aio import AsyncRedisSaver
                self._checkpointer = AsyncRedisSaver.from_conn_string(_redis_url)
                logger.info("ManifestExecutor using Redis checkpointer (%s)", _redis_url.split("@")[-1])
            except Exception as _exc:
                logger.warning(
                    "Redis checkpointer unavailable (%s) — falling back to MemorySaver", _exc
                )
                self._checkpointer = MemorySaver()
        else:
            self._checkpointer = MemorySaver()

    # ── Registration ────────────────────────────────────────────────

    def register_pre_step(self, name: str, fn: Callable) -> "ManifestExecutor":
        """Register a node to execute before classification (e.g. DB lookup).

        Steps are executed in registration order, then the classifier runs.
        Returns self for fluent chaining.
        """
        self._pre_steps.append((name, fn))
        return self

    def register_tool(self, name: str, fn: Callable, schema: dict) -> "ManifestExecutor":
        """Register a callable tool that specialist agents can invoke via function calling.

        fn     — async or sync callable; called with the keyword args the LLM provides.
                 May return a str, or a dict {"content": str, "state": dict} to also
                 merge keys back into the graph state.
        schema — OpenAI function schema dict (keys: name, description, parameters).
                 Validated against ToolSchema; raises ValueError if malformed.
        Returns self for fluent chaining.
        """
        from pydantic import ValidationError
        try:
            validated = ToolSchema.model_validate(schema)
        except ValidationError as exc:
            raise ValueError(f"Invalid tool schema for '{name}': {exc}") from exc
        self._tools[name] = (fn, validated)
        return self

    def set_token_stream(self, trace_id: str, queue: asyncio.Queue) -> None:
        """Register a queue to receive token events for a specific trace.

        The executor pushes {"node": name, "token": str} for each token and
        {"node": name, "done": True} when a node finishes streaming.
        Call before astream() / ainvoke() for that trace.
        """
        self._token_queues[trace_id] = queue

    def clear_token_stream(self, trace_id: str) -> None:
        self._token_queues.pop(trace_id, None)

    def cancel_stream(self, trace_id: str) -> None:
        """Request cancellation of an active astream() run.

        The executor checks this flag between LangGraph node boundaries.
        The current node finishes cleanly before the stream exits, so partial
        state is always at a consistent node boundary.
        """
        event = self._cancel_events.get(trace_id)
        if event:
            event.set()
            logger.info("Cancellation requested for trace %s", trace_id)

    def is_cancelled(self, trace_id: str) -> bool:
        """Return True if cancellation has been requested for trace_id."""
        event = self._cancel_events.get(trace_id)
        return event is not None and event.is_set()

    # ── Tool helpers ─────────────────────────────────────────────────

    def _tools_list(self) -> list[dict] | None:
        """Return the OpenAI tools array for all registered tools, or None if empty."""
        if not self._tools:
            return None
        return [{"type": "function", "function": ts.to_openai_function()} for _, ts in self._tools.values()]

    def _tools_list_for_agent(self, cfg: AgentConfig) -> list[dict] | None:
        """Return the OpenAI tools array filtered to only tools the agent may use.

        If ``cfg.tools`` is non-empty, only those named tools are exposed to the
        agent (enforcing per-agent tool access control declared in the manifest).
        If ``cfg.tools`` is empty, all registered tools are available (default).
        Returns None if no tools are available for this agent.
        """
        if not self._tools:
            return None
        allowed = set(cfg.tools) if cfg.tools else set(self._tools.keys())
        tools = [
            {"type": "function", "function": ts.to_openai_function()}
            for name, (_, ts) in self._tools.items()
            if name in allowed
        ]
        return tools or None

    async def _execute_tool_call(
        self,
        call_id: str,
        tool_name: str,
        arguments_json: str,
        state: dict,
        lf_trace: Any | None,
    ) -> tuple[dict, dict]:
        """Execute a registered tool; return (tool_message, state_update)."""
        if tool_name not in self._tools:
            msg = {"role": "tool", "tool_call_id": call_id, "content": f"Unknown tool: {tool_name}"}
            return msg, {}

        fn, _schema = self._tools[tool_name]
        try:
            args = _json.loads(arguments_json) if arguments_json else {}
        except Exception:
            args = {}

        lf_span = None
        if lf_trace:
            try:
                lf_span = lf_trace.span(name=f"tool-{tool_name}", input=args)
            except Exception:
                pass

        trace_id = state.get("trace_id", "")
        token_queue = self._token_queues.get(trace_id)

        try:
            raw = await fn(**args) if asyncio.iscoroutinefunction(fn) else fn(**args)
        except Exception as exc:
            logger.warning("Tool %s raised: %s", tool_name, exc, exc_info=True)
            raw = f"Error executing {tool_name}: {exc}"

        # Tools may return plain str or {"content": str, "state": dict}
        if isinstance(raw, dict) and "content" in raw:
            content_str = str(raw["content"])
            state_update = raw.get("state", {})
        else:
            content_str = str(raw)
            state_update = {}

        # Validate tool result shape before merging into state
        ToolResult(tool_name=tool_name, call_id=call_id, content=content_str, state_update=state_update)

        if lf_span:
            try:
                lf_span.end(output=content_str)
            except Exception:
                pass

        # Notify queue that tool call completed — include args + result summary for UI detail
        if token_queue:
            try:
                token_queue.put_nowait({
                    "node": f"tool:{tool_name}",
                    "done": True,
                    "detail": {
                        "args": args,
                        "found": bool(state_update.get("taxpayer")),
                        "taxpayer_name": (state_update.get("taxpayer") or {}).get("full_name"),
                        "taxpayer_id": (state_update.get("taxpayer") or {}).get("tax_id"),
                    },
                })
            except asyncio.QueueFull:
                pass

        msg = {"role": "tool", "tool_call_id": call_id, "content": content_str}
        return msg, state_update

    # ── Compile ─────────────────────────────────────────────────────

    def compile(self, state_schema: type = RouterState) -> Any:
        """Build and compile the LangGraph from the manifest.

        The graph is compiled with the shared checkpointer (Redis or MemorySaver) so
        that state is saved after every node.  Pass thread_id in the LangGraph config
        (via astream / ainvoke) to enable resume after interruption.

        Returns the compiled graph (same object returned by StateGraph.compile()).
        Also stored as self._compiled for use by astream()/ainvoke().
        """
        if self._manifest.pattern == "router":
            return self._compile_router(state_schema)
        if self._manifest.pattern in ("concurrent", "magentic"):
            return self._compile_concurrent(state_schema)
        if self._manifest.pattern == "supervisor":
            return self._compile_supervisor(state_schema)
        raise NotImplementedError(
            f"Pattern '{self._manifest.pattern}' not yet supported by ManifestExecutor. "
            "Supported: 'router', 'concurrent', 'supervisor'. 'magentic' is an alias for 'concurrent'. "
            "For 'linear', 'planner', register your graph via LangGraphEngine."
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

        self._compiled = graph.compile(checkpointer=self._checkpointer)
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

        self._compiled = graph.compile(checkpointer=self._checkpointer)
        logger.info(
            "ManifestExecutor compiled '%s' (concurrent pattern, %d intent agents, %d pre-steps)",
            self._manifest.app_id, len(specialists_cfg), len(self._pre_steps),
        )
        return self._compiled

    # ── Streaming / invocation ──────────────────────────────────────

    async def astream(self, state: dict, **kwargs) -> AsyncGenerator:
        """Stream workflow updates. Opens/closes a Langfuse trace around the run.

        Passes thread_id = state["email_id"] (or trace_id) to the LangGraph
        checkpointer so that state is saved after every node and a cancelled run
        can be resumed by calling astream() again with the same state.

        Yields the same chunks as compiled_graph.astream().
        Raises CancelledError-equivalent by stopping iteration if cancel_stream()
        is called — the current node always finishes cleanly first.
        """
        if self._compiled is None:
            raise RuntimeError("Call compile() before astream().")

        trace_id: str = state.get("trace_id") or str(uuid.uuid4())
        thread_id: str = state.get("email_id") or trace_id

        # Register cancel event for this run
        cancel_event = asyncio.Event()
        self._cancel_events[trace_id] = cancel_event

        self._open_trace(trace_id, state)

        # thread_id enables the checkpointer to save state per email and resume
        # after interruption (Redis) or within the same process (MemorySaver).
        lg_config = {"configurable": {"thread_id": thread_id}}

        try:
            async for chunk in self._compiled.astream(state, config=lg_config, **kwargs):
                yield chunk
                # Check cancellation between node boundaries (after each node completes)
                if cancel_event.is_set():
                    logger.info(
                        "Stream cancelled at node boundary for trace %s (thread %s)",
                        trace_id, thread_id,
                    )
                    break
        finally:
            self._close_trace(trace_id, state)
            self.clear_token_stream(trace_id)
            self._cancel_events.pop(trace_id, None)
            # Signal end of stream to any token queue consumer
            q = self._token_queues.get(trace_id)
            if q:
                try:
                    q.put_nowait(None)  # sentinel: stream ended
                except asyncio.QueueFull:
                    pass

    async def ainvoke(self, state: dict, **kwargs) -> dict:
        """Invoke workflow and return final state. Manages Langfuse trace lifecycle."""
        if self._compiled is None:
            raise RuntimeError("Call compile() before ainvoke().")

        trace_id: str = state.get("trace_id") or str(uuid.uuid4())
        thread_id: str = state.get("email_id") or trace_id
        lg_config = {"configurable": {"thread_id": thread_id}}
        self._open_trace(trace_id, state)

        try:
            result = await self._compiled.ainvoke(state, config=lg_config, **kwargs)
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
                "messages": state.get("messages", []) + [
                    AgentMessage(role="classifier", content=route, agent_name=cfg.name).to_dict()
                ],
            }

        return node_classify

    def _make_specialist_node(self, cfg: AgentConfig, categories: list[str]) -> Callable:
        """Return an async node function for a specialist agent.

        Supports:
        - Tool calling: if tools are registered, passes them to the LLM and executes
          any tool_calls in a loop before producing the final reply.
        - Token streaming: if a queue is registered for the trace_id, streams tokens
          and pushes them to the queue.
        - State merging: tool results that return {"state": {...}} are merged into the
          graph state (e.g. taxpayer record for HITL condition evaluation).
        """
        llm = self._llm
        executor = self

        async def node_specialist(state: dict) -> dict:
            # Build system prompt: optional context prefix + base prompt + SOP
            parts: list[str] = []
            ctx = state.get("_context", "")
            allowed_tools = executor._tools_list_for_agent(cfg)
            # Only prepend _context if no tools available for this agent
            if ctx and not allowed_tools:
                parts.append(ctx)
            # Instruct the LLM about the tools it is allowed to use
            if allowed_tools:
                tool_names = [t["function"]["name"] for t in allowed_tools]
                parts.append(
                    f"You have access to the following tools: {', '.join(tool_names)}.\n"
                    "When the email contains a Tax Identification Number (TIN, format SG-T###-####), "
                    "ALWAYS call 'lookup_taxpayer' BEFORE drafting your reply.\n"
                    "Never guess or invent taxpayer data — only use the information returned by the tool."
                )
            parts.append(cfg.system_prompt)
            if cfg.sop:
                parts.append(f"SOP YOU MUST FOLLOW:\n{cfg.sop}")
            system_prompt = "\n\n".join(parts)

            messages: list[dict] = [
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

            tool_kwargs = {}
            if allowed_tools:
                tool_kwargs["tools"] = allowed_tools

            # ── Tool-calling loop ──────────────────────────────────
            extra_state: dict = {}
            max_tool_rounds = 5
            for _round in range(max_tool_rounds):
                resp = await llm.complete(messages=messages, temperature=cfg.temperature, **tool_kwargs)

                if resp.tool_calls:
                    # Append assistant message with tool_call requests
                    messages.append({
                        "role": "assistant",
                        "tool_calls": [
                            {"id": tc["id"], "type": "function",
                             "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                            for tc in resp.tool_calls
                        ],
                    })
                    # Execute each tool and append results
                    for tc in resp.tool_calls:
                        tool_msg, state_update = await executor._execute_tool_call(
                            tc["id"], tc["name"], tc["arguments"], state, lf_trace
                        )
                        messages.append(tool_msg)
                        extra_state.update(state_update)
                    # Loop: ask LLM to continue with tool results
                    continue

                # No tool calls — we have the final text response
                break

            # ── Token streaming (if queue registered) ─────────────
            token_queue = executor._token_queues.get(state.get("trace_id", ""))

            # Extract CoT reasoning if show_reasoning is enabled
            raw_content = resp.content or ""
            thinking_text = ""
            if cfg.show_reasoning:
                think_match = _re.search(r"<think>(.*?)</think>", raw_content, _re.DOTALL)
                if think_match:
                    thinking_text = think_match.group(1).strip()
                    raw_content = _re.sub(
                        r"<think>.*?</think>", "", raw_content, flags=_re.DOTALL
                    ).strip()

            if token_queue and not resp.tool_calls:
                # Emit reasoning block before tokens if present
                if thinking_text:
                    try:
                        token_queue.put_nowait({"node": cfg.name, "reasoning": thinking_text})
                    except asyncio.QueueFull:
                        pass

                if cfg.show_reasoning:
                    # Push final content word-by-word (no second LLM call needed)
                    for word in raw_content.split(" "):
                        if word:
                            try:
                                token_queue.put_nowait({"node": cfg.name, "token": word + " "})
                            except asyncio.QueueFull:
                                pass
                            await asyncio.sleep(0.02)
                    resp_content = raw_content
                elif raw_content:
                    # Default path: re-stream via LLM for live token-by-token display
                    tokens: list[str] = []
                    async for token in llm.complete_stream(
                        messages=messages,
                        temperature=cfg.temperature,
                    ):
                        tokens.append(token)
                        try:
                            token_queue.put_nowait({"node": cfg.name, "token": token})
                        except asyncio.QueueFull:
                            pass
                    resp_content = "".join(tokens) if tokens else raw_content
                else:
                    resp_content = raw_content
                try:
                    token_queue.put_nowait({"node": cfg.name, "done": True})
                except asyncio.QueueFull:
                    pass
            else:
                resp_content = raw_content

            if lf_gen:
                try:
                    lf_gen.end(
                        output=resp_content,
                        usage={
                            "input": resp.usage.get("prompt_tokens", resp.usage.get("input_tokens", 0)),
                            "output": resp.usage.get("completion_tokens", resp.usage.get("output_tokens", 0)),
                        },
                    )
                except Exception:
                    pass

            result: dict = {
                "output": resp_content,
                "messages": state.get("messages", []) + [
                    AgentMessage(role="agent", content=resp_content, agent_name=cfg.name).to_dict()
                ],
                **extra_state,
            }

            # Evaluate HITL condition from manifest
            if cfg.hitl_condition:
                try:
                    tp = extra_state.get("taxpayer") or state.get("taxpayer")
                    hitl = bool(eval(  # noqa: S307 — developer-authored manifest config only
                        cfg.hitl_condition,
                        {"__builtins__": _EVAL_BUILTINS},
                        {"state": state, "taxpayer": tp, "output": resp_content},
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
                    AgentMessage(role="intent_classifier", content=", ".join(intents), agent_name=cfg.name).to_dict()
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
            allowed_tools = executor._tools_list_for_agent(cfg)
            if ctx and not allowed_tools:
                parts.append(ctx)
            if allowed_tools:
                tool_names = [t["function"]["name"] for t in allowed_tools]
                parts.append(
                    f"You have access to the following tools: {', '.join(tool_names)}.\n"
                    "When the email contains a Tax Identification Number (TIN, format SG-T###-####), "
                    "ALWAYS call 'lookup_taxpayer' BEFORE drafting your reply.\n"
                    "Never guess or invent taxpayer data — only use the information returned by the tool."
                )
            parts.append(cfg.system_prompt)
            if cfg.sop:
                parts.append(f"SOP YOU MUST FOLLOW:\n{cfg.sop}")
            system_prompt = "\n\n".join(parts)

            messages: list[dict] = [
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

            tool_kwargs = {}
            if allowed_tools:
                tool_kwargs["tools"] = allowed_tools

            extra_state: dict = {}
            for _round in range(5):
                resp = await executor._llm.complete(messages=messages, temperature=cfg.temperature, **tool_kwargs)
                if resp.tool_calls:
                    messages.append({
                        "role": "assistant",
                        "tool_calls": [
                            {"id": tc["id"], "type": "function",
                             "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                            for tc in resp.tool_calls
                        ],
                    })
                    for tc in resp.tool_calls:
                        tool_msg, state_update = await executor._execute_tool_call(
                            tc["id"], tc["name"], tc["arguments"], state, lf_trace
                        )
                        messages.append(tool_msg)
                        extra_state.update(state_update)
                    continue
                break

            if lf_gen:
                try:
                    lf_gen.end(
                        output=resp.content,
                        usage={
                            "input": resp.usage.get("prompt_tokens", resp.usage.get("input_tokens", 0)),
                            "output": resp.usage.get("completion_tokens", resp.usage.get("output_tokens", 0)),
                        },
                    )
                except Exception:
                    pass

            result: dict = {
                "output": resp.content,
                "hitl_required": False,
                "policy_flags": [],
                "hitl_action": "",
                **extra_state,
            }

            if cfg.hitl_condition:
                try:
                    tp = extra_state.get("taxpayer") or state.get("taxpayer")
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

            # Propagate taxpayer (and _context) from first specialist that resolved it
            taxpayer = next((r["taxpayer"] for r in results if r.get("taxpayer")), state.get("taxpayer"))
            ctx_update = next((r["_context"] for r in results if r.get("_context")), state.get("_context", ""))

            out: dict = {
                "specialist_outputs": specialist_outputs,
                "intents": valid,
                "hitl_required": hitl_required,
                "hitl_action": hitl_action,
                "policy_flags": all_flags,
                "messages": state.get("messages", []) + [
                    {"role": "dispatch", "content": f"Dispatched to: {', '.join(valid)}"}
                ],
                "taxpayer": taxpayer,
                "_context": ctx_update,
            }
            return out

        return node_dispatch

    def _make_merge_node(self) -> Callable:
        """Return an async node that merges specialist outputs into a single reply.

        For a single intent, returns that specialist's output directly.
        For multiple intents, calls the LLM to synthesise a unified reply.
        """
        llm = self._llm
        executor = self

        async def node_merge(state: dict) -> dict:
            specialist_outputs: dict = state.get("specialist_outputs", {})

            lf_trace = executor.get_trace(state.get("trace_id", ""))
            lf_gen = None

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

                if lf_trace:
                    try:
                        lf_gen = lf_trace.generation(
                            name="merge",
                            model=getattr(llm, "default_model", "unknown"),
                            input=merge_messages,
                            metadata={
                                "pattern": "concurrent",
                                "merged_specialists": list(specialist_outputs.keys()),
                                "specialist_count": len(specialist_outputs),
                            },
                        )
                    except Exception:
                        pass

                resp = await llm.complete(messages=merge_messages, temperature=0.1)
                output = resp.content

                if lf_gen:
                    try:
                        lf_gen.end(
                            output=output,
                            usage={
                                "input": resp.usage.get("input_tokens", 0),
                                "output": resp.usage.get("output_tokens", 0),
                            },
                        )
                    except Exception:
                        pass

            return {
                "output": output,
                "messages": state.get("messages", []) + [
                    AgentMessage(role="merge", content=output).to_dict()
                ],
            }

        return node_merge

    def _compile_supervisor(self, state_schema: type) -> Any:
        """Build the supervisor pattern: [pre-steps] → supervisor → specialist (loop) → END.

        The supervisor agent reads the request, decides which specialist to invoke
        (by name), receives its output, then decides the next specialist or "FINISH".
        Corresponds to Microsoft's 'Orchestrator' / 'Magentic-One orchestrator' pattern.

        Manifest convention: the agent whose name is 'supervisor', or the first agent
        if none has that name, is used as the orchestrator.  All other agents are
        specialist candidates.
        """
        agents = {a.name: a for a in self._manifest.agents}

        # Identify supervisor vs specialists
        supervisor_cfg = agents.get("supervisor") or self._manifest.agents[0]
        specialists_cfg = {k: v for k, v in agents.items() if k != supervisor_cfg.name}
        specialist_names = list(specialists_cfg.keys())

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

        # ── Supervisor node ────────────────────────────────────────
        graph.add_node("supervisor", self._make_supervisor_node(supervisor_cfg, specialist_names))
        if prev:
            graph.add_edge(prev, "supervisor")
        else:
            graph.set_entry_point("supervisor")

        # ── Specialist nodes (each loops back to supervisor) ───────
        for name, cfg in specialists_cfg.items():
            graph.add_node(name, self._make_supervisor_specialist_node(cfg))
            graph.add_edge(name, "supervisor")

        # ── Conditional routing from supervisor ────────────────────
        def _route_supervisor(state: dict) -> str:
            nxt = state.get("next_agent", "")
            return nxt if nxt in specialists_cfg else END

        graph.add_conditional_edges(
            "supervisor",
            _route_supervisor,
            {name: name for name in specialists_cfg} | {END: END},
        )

        self._compiled = graph.compile(checkpointer=self._checkpointer)
        logger.info(
            "ManifestExecutor compiled '%s' (supervisor pattern, %d specialists, %d pre-steps)",
            self._manifest.app_id, len(specialists_cfg), len(self._pre_steps),
        )
        return self._compiled

    def _make_supervisor_node(self, cfg: AgentConfig, specialist_names: list[str]) -> Callable:
        """Return an async node that decides which specialist to invoke next (or FINISH)."""
        llm = self._llm
        executor = self
        specialists_text = "\n".join(f"  - {name}" for name in specialist_names)

        async def node_supervisor(state: dict) -> dict:
            specialist_outputs: dict = state.get("specialist_outputs", {})

            system_prompt = cfg.system_prompt.replace("{specialists}", specialists_text)
            messages: list[dict] = [{"role": "system", "content": system_prompt}]
            messages.append({"role": "user", "content": state["input"]})

            # Give the supervisor visibility into what specialists have already said
            for sp_name, sp_output in specialist_outputs.items():
                messages.append({
                    "role": "assistant",
                    "content": f"[{sp_name} specialist response]:\n{sp_output}",
                })

            lf_trace = executor.get_trace(state.get("trace_id", ""))
            if lf_trace:
                try:
                    lf_gen = lf_trace.generation(
                        name=f"supervisor-step-{len(specialist_outputs)}",
                        model=getattr(llm, "default_model", cfg.model),
                        input=messages,
                        metadata={"pattern": "supervisor", "step": len(specialist_outputs)},
                    )
                except Exception:
                    lf_gen = None
            else:
                lf_gen = None

            resp = await llm.complete(messages=messages, temperature=cfg.temperature)
            decision = resp.content.strip().lower().replace(" ", "_").strip(".")

            if lf_gen:
                try:
                    lf_gen.end(output=decision)
                except Exception:
                    pass

            logger.info("Supervisor decided: '%s' (trace %s)", decision, state.get("trace_id", "?"))

            # Push supervisor decision to token queue so it appears in real-time in the UI
            # (rather than landing at the end via graph_steps after specialists finish)
            token_queue = executor._token_queues.get(state.get("trace_id", ""))
            if token_queue:
                try:
                    token_queue.put_nowait({
                        "node": "supervisor",
                        "done": True,
                        "detail": {"next": decision if decision not in ("finish",) and decision in specialist_names else "FINISH"},
                    })
                except asyncio.QueueFull:
                    pass

            if decision == "finish" or decision not in specialist_names:
                # Merge specialist outputs through LLM if more than one, same as merge node
                if specialist_outputs:
                    if len(specialist_outputs) == 1:
                        final_output = next(iter(specialist_outputs.values()))
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
                        lf_trace = executor.get_trace(state.get("trace_id", ""))
                        lf_merge = None
                        if lf_trace:
                            try:
                                lf_merge = lf_trace.generation(
                                    name="supervisor-merge",
                                    model=getattr(llm, "default_model", cfg.model),
                                    input=merge_messages,
                                    metadata={"pattern": "supervisor", "merged_specialists": list(specialist_outputs.keys())},
                                )
                            except Exception:
                                pass
                        merge_resp = await llm.complete(messages=merge_messages, temperature=0.1)
                        final_output = merge_resp.content
                        if lf_merge:
                            try:
                                lf_merge.end(output=final_output)
                            except Exception:
                                pass
                else:
                    final_output = state.get("output", "")

                return {
                    "next_agent": "FINISH",
                    "output": final_output,
                    "messages": state.get("messages", []) + [
                        AgentMessage(role="supervisor", content="FINISH", agent_name=cfg.name).to_dict()
                    ],
                }

            return {
                "next_agent": decision,
                "messages": state.get("messages", []) + [
                    AgentMessage(role="supervisor", content=decision, agent_name=cfg.name).to_dict()
                ],
            }

        return node_supervisor

    def _make_supervisor_specialist_node(self, cfg: AgentConfig) -> Callable:
        """Specialist node for the supervisor pattern.

        Same as the standard specialist node but accumulates output in
        specialist_outputs[name] rather than overwriting the top-level output.
        """
        base_node = self._make_specialist_node(cfg, [])

        async def node_supervisor_specialist(state: dict) -> dict:
            result = await base_node(state)
            specialist_outputs = dict(state.get("specialist_outputs") or {})
            specialist_outputs[cfg.name] = result.get("output", "")
            result["specialist_outputs"] = specialist_outputs
            return result

        return node_supervisor_specialist

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
