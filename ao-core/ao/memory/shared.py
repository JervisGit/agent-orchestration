"""Shared inter-agent state and communication.

Provides two mechanisms for agents to share data:
1. SharedState — in-process dict for agents within the same workflow
2. MessageBus — async message passing via Azure Service Bus for cross-workflow comms
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class SharedState:
    """In-process shared state for agents within a single workflow.

    Thread-safe dict wrapper. Agents can read/write shared context
    without passing data through the graph state explicitly.
    """

    def __init__(self):
        self._store: dict[str, dict[str, Any]] = {}

    def get_namespace(self, workflow_id: str) -> dict[str, Any]:
        if workflow_id not in self._store:
            self._store[workflow_id] = {}
        return self._store[workflow_id]

    def set(self, workflow_id: str, key: str, value: Any) -> None:
        ns = self.get_namespace(workflow_id)
        ns[key] = value

    def get(self, workflow_id: str, key: str, default: Any = None) -> Any:
        return self.get_namespace(workflow_id).get(key, default)

    def clear(self, workflow_id: str) -> None:
        self._store.pop(workflow_id, None)


class MessageBus:
    """Async message passing between agents via Azure Service Bus.

    Used for cross-workflow communication (e.g., one workflow triggers
    another, or agents in different workflows share findings).
    """

    def __init__(self, connection_string: str | None = None):
        self._conn_str = connection_string
        self._local_queue: list[dict[str, Any]] = []  # Fallback for local dev

    async def publish(
        self,
        topic: str,
        message: dict[str, Any],
        sender_workflow_id: str = "",
    ) -> None:
        """Publish a message to a topic."""
        envelope = {
            "topic": topic,
            "sender_workflow_id": sender_workflow_id,
            "payload": message,
        }

        if self._conn_str:
            from azure.servicebus.aio import ServiceBusClient

            async with ServiceBusClient.from_connection_string(self._conn_str) as client:
                sender = client.get_topic_sender(topic_name=topic)
                async with sender:
                    from azure.servicebus import ServiceBusMessage

                    await sender.send_messages(
                        ServiceBusMessage(body=json.dumps(envelope))
                    )
            logger.info("Published message to Service Bus topic '%s'", topic)
        else:
            # Local dev fallback — in-memory queue
            self._local_queue.append(envelope)
            logger.info("Published message to local queue (topic=%s)", topic)

    async def consume_local(self, topic: str) -> list[dict[str, Any]]:
        """Consume messages from local queue (dev only)."""
        msgs = [m for m in self._local_queue if m["topic"] == topic]
        self._local_queue = [m for m in self._local_queue if m["topic"] != topic]
        return msgs
