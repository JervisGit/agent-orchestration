"""Dead-letter queue processor — retry or alert on failed workflow steps.

Consumes messages from the dead-letter queue (Azure Service Bus DLQ)
and either retries them or sends alerts.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DeadLetterMessage:
    """A message from the dead-letter queue."""

    message_id: str
    workflow_id: str
    step_name: str
    error: str
    retry_count: int = 0
    payload: dict[str, Any] | None = None


class DeadLetterProcessor:
    """Processes dead-letter messages with configurable retry and alerting."""

    def __init__(
        self,
        max_retries: int = 3,
        alert_callback=None,
        connection_string: str | None = None,
    ):
        self._max_retries = max_retries
        self._alert_callback = alert_callback
        self._conn_str = connection_string
        self._local_queue: list[DeadLetterMessage] = []

    def enqueue_local(self, msg: DeadLetterMessage) -> None:
        """Add a message to the local queue (dev mode)."""
        self._local_queue.append(msg)

    async def process_batch(self) -> list[dict[str, Any]]:
        """Process all messages in the local queue."""
        results = []
        while self._local_queue:
            msg = self._local_queue.pop(0)
            result = await self._process_one(msg)
            results.append(result)
        return results

    async def _process_one(self, msg: DeadLetterMessage) -> dict[str, Any]:
        if msg.retry_count < self._max_retries:
            msg.retry_count += 1
            logger.info(
                "Retrying dead-letter message %s (attempt %d/%d): %s",
                msg.message_id,
                msg.retry_count,
                self._max_retries,
                msg.error,
            )
            # TODO: re-submit to engine for retry
            return {"message_id": msg.message_id, "action": "retry", "attempt": msg.retry_count}
        else:
            logger.warning(
                "Dead-letter message %s exhausted retries, alerting: %s",
                msg.message_id,
                msg.error,
            )
            if self._alert_callback:
                await self._alert_callback(msg)
            return {"message_id": msg.message_id, "action": "alerted"}

    async def run_service_bus_consumer(self, queue_name: str = "ao-dead-letter") -> None:
        """Long-running consumer for Azure Service Bus DLQ (production)."""
        if not self._conn_str:
            logger.warning("No Service Bus connection, running in local mode")
            return

        from azure.servicebus.aio import ServiceBusClient

        async with ServiceBusClient.from_connection_string(self._conn_str) as client:
            receiver = client.get_queue_receiver(
                queue_name=queue_name,
                sub_queue="deadletter",
            )
            async with receiver:
                async for message in receiver:
                    body = json.loads(str(message))
                    msg = DeadLetterMessage(
                        message_id=message.message_id or "",
                        workflow_id=body.get("workflow_id", ""),
                        step_name=body.get("step_name", ""),
                        error=body.get("error", ""),
                        retry_count=body.get("retry_count", 0),
                        payload=body.get("payload"),
                    )
                    await self._process_one(msg)
                    await receiver.complete_message(message)
