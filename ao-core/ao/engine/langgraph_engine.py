"""LangGraph-based orchestration engine implementation."""

import logging
from typing import Any

from langgraph.graph import StateGraph

from ao.engine.base import OrchestrationEngine, WorkflowConfig, WorkflowResult
from ao.hitl.manager import ApprovalMode, ApprovalStatus, HITLManager
from ao.observability.tracer import AOTracer
from ao.resilience.checkpoint import CheckpointerType, create_checkpointer
from ao.resilience.fallback import FallbackHandler
from ao.resilience.retry import RetryPolicy

logger = logging.getLogger(__name__)


class LangGraphEngine(OrchestrationEngine):
    """Orchestration engine backed by LangGraph state graphs."""

    def __init__(
        self,
        tracer: AOTracer | None = None,
        checkpointer_type: CheckpointerType = CheckpointerType.MEMORY,
        postgres_conn: str | None = None,
        hitl_manager: HITLManager | None = None,
        fallback_handler: FallbackHandler | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        self._graphs: dict[str, StateGraph] = {}
        self._compiled: dict[str, Any] = {}
        self._checkpointer = create_checkpointer(checkpointer_type, postgres_conn)
        self._tracer = tracer
        self._hitl = hitl_manager
        self._fallback = fallback_handler or FallbackHandler()
        self._retry_policy = retry_policy or RetryPolicy(max_retries=0)
        # Steps that require HITL approval before execution
        self._hitl_steps: dict[str, dict[str, ApprovalMode]] = {}

    def register_graph(
        self,
        workflow_id: str,
        graph: StateGraph,
        hitl_steps: dict[str, ApprovalMode] | None = None,
        interrupt_before: list[str] | None = None,
    ) -> None:
        """Register a LangGraph state graph for a workflow.

        Args:
            workflow_id: Unique workflow identifier.
            graph: The LangGraph StateGraph.
            hitl_steps: Map of step_name -> ApprovalMode for HITL gates.
            interrupt_before: LangGraph interrupt_before nodes (for native HITL).
        """
        self._graphs[workflow_id] = graph
        compile_kwargs: dict[str, Any] = {"checkpointer": self._checkpointer}
        if interrupt_before:
            compile_kwargs["interrupt_before"] = interrupt_before
        self._compiled[workflow_id] = graph.compile(**compile_kwargs)
        if hitl_steps:
            self._hitl_steps[workflow_id] = hitl_steps

    async def run(
        self, config: WorkflowConfig, input_data: dict[str, Any]
    ) -> WorkflowResult:
        compiled = self._compiled.get(config.workflow_id)
        if not compiled:
            return WorkflowResult(
                workflow_id=config.workflow_id,
                status="failed",
                error=f"No graph registered for workflow '{config.workflow_id}'",
            )

        span = None
        if self._tracer:
            span = self._tracer.start_span(
                f"workflow:{config.workflow_id}", metadata=config.metadata
            )

        try:
            # Pre-HITL check: if workflow has HITL steps and manager is configured
            hitl_steps = self._hitl_steps.get(config.workflow_id, {})
            if hitl_steps and self._hitl and config.hitl_enabled:
                for step_name, mode in hitl_steps.items():
                    approval = await self._hitl.request_approval(
                        workflow_id=config.workflow_id,
                        step_name=step_name,
                        payload={"input": input_data, "step": step_name},
                        mode=mode,
                    )
                    if approval.status == ApprovalStatus.REJECTED:
                        if self._tracer and span:
                            self._tracer.end_span(span, status="rejected")
                        return WorkflowResult(
                            workflow_id=config.workflow_id,
                            status="rejected",
                            error=f"Step '{step_name}' rejected by {approval.reviewer}: {approval.resolution_note}",
                        )
                    if approval.status == ApprovalStatus.TIMED_OUT:
                        if self._tracer and span:
                            self._tracer.end_span(span, status="timed_out")
                        return WorkflowResult(
                            workflow_id=config.workflow_id,
                            status="timed_out",
                            error=f"HITL approval timed out for step '{step_name}'",
                        )

            thread_config = {"configurable": {"thread_id": config.workflow_id}}
            result = await compiled.ainvoke(input_data, config=thread_config)

            if self._tracer and span:
                self._tracer.end_span(span, status="completed")
            return WorkflowResult(
                workflow_id=config.workflow_id,
                status="completed",
                output=result if isinstance(result, dict) else {"result": result},
            )
        except Exception as e:
            logger.exception("Workflow %s failed", config.workflow_id)
            if self._tracer and span:
                self._tracer.end_span(span, status="failed", error=str(e))
            return WorkflowResult(
                workflow_id=config.workflow_id,
                status="failed",
                error=str(e),
            )

    async def resume(
        self, workflow_id: str, checkpoint_id: str
    ) -> WorkflowResult:
        compiled = self._compiled.get(workflow_id)
        if not compiled:
            return WorkflowResult(
                workflow_id=workflow_id,
                status="failed",
                error=f"No graph registered for workflow '{workflow_id}'",
            )

        try:
            thread_config = {
                "configurable": {
                    "thread_id": workflow_id,
                    "checkpoint_id": checkpoint_id,
                }
            }
            result = await compiled.ainvoke(None, config=thread_config)
            return WorkflowResult(
                workflow_id=workflow_id,
                status="completed",
                output=result if isinstance(result, dict) else {"result": result},
            )
        except Exception as e:
            logger.exception("Resume %s failed", workflow_id)
            return WorkflowResult(
                workflow_id=workflow_id, status="failed", error=str(e)
            )