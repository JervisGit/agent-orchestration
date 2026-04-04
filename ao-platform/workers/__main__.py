"""Worker entry point — runs dead-letter processor and eval runner as background services."""

import asyncio
import logging
import os

from workers.dead_letter import DeadLetterProcessor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    conn_str = os.getenv("SERVICEBUS_CONNECTION_STRING")
    dlq_queue = os.getenv("SERVICEBUS_DLQ_QUEUE", "ao-dead-letter")

    processor = DeadLetterProcessor(
        max_retries=int(os.getenv("DLQ_MAX_RETRIES", "3")),
        connection_string=conn_str,
    )

    logger.info("AO worker starting — DLQ queue: %s", dlq_queue)

    if not conn_str:
        logger.warning(
            "SERVICEBUS_CONNECTION_STRING not set — worker idle (set env var to enable)"
        )
        # Keep container alive so ACA doesn't crash-loop the revision.
        while True:
            await asyncio.sleep(60)

    await processor.run_service_bus_consumer(queue_name=dlq_queue)
    logger.info("AO worker shut down")


if __name__ == "__main__":
    asyncio.run(main())
