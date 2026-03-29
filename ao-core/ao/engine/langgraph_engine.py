"""LangGraph-based orchestration engine implementation."""

import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph

from ao.engine.base import OrchestrationEngine, WorkflowConfig, WorkflowResult
from ao.observability.tracer import AOTracer

logger = logging.getLogger(__name__)


class LangGraphEngine(OrchestrationEngine):
    """Orchestration engine backed by LangGraph state graphs."""

    def __init__(self, tracer: AOTracer | None = None):
        self._graphs: dict[str, StateGraph] = {}
        self._compiled: dict[str, Any] = {}
        self._checkpointer = MemorySaver()
        self._tracer = tracer

    def register_graph(self, workflow_id: str, graph: StateGraph) -> None:
        """Register a LangGraph state graph for a workflow."""
        self._graphs[workflow_id] = graph
        self._compiled[workflow_id] = graph.compile(
            checkpointer=self._checkpointer
        )

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