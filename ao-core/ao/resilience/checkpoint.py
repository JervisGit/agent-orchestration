"""LangGraph checkpointing wrapper for workflow state persistence.

Provides a factory that returns the appropriate checkpointer:
- MemorySaver for local dev/testing
- PostgresSaver for production (durable, survives restarts)
"""

import logging
from enum import Enum

from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)


class CheckpointerType(Enum):
    MEMORY = "memory"
    POSTGRES = "postgres"


def create_checkpointer(
    backend: CheckpointerType = CheckpointerType.MEMORY,
    connection_string: str | None = None,
):
    """Factory for LangGraph checkpointers.

    Args:
        backend: Which checkpointer backend to use.
        connection_string: PostgreSQL connection string (required for postgres backend).

    Returns:
        A LangGraph-compatible checkpointer instance.
    """
    if backend == CheckpointerType.MEMORY:
        logger.info("Using in-memory checkpointer (non-durable)")
        return MemorySaver()

    if backend == CheckpointerType.POSTGRES:
        if not connection_string:
            raise ValueError("PostgreSQL connection_string required for postgres checkpointer")
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            logger.info("Using PostgreSQL async checkpointer (durable)")
            return AsyncPostgresSaver.from_conn_string(connection_string)
        except ImportError:
            logger.warning(
                "langgraph-checkpoint-postgres not installed, falling back to memory. "
                "Install with: pip install langgraph-checkpoint-postgres"
            )
            return MemorySaver()

    raise ValueError(f"Unknown checkpointer backend: {backend}")
