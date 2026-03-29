"""WebSocket notification channel for HITL approvals.

Broadcasts approval requests to connected WebSocket clients
(e.g., the AO Dashboard). Uses an in-process broadcast pattern;
in production, would integrate with the FastAPI WebSocket endpoint.
"""

import json
import logging
from typing import Any

from ao.hitl.manager import ApprovalRequest, HITLChannel

logger = logging.getLogger(__name__)


class WebSocketChannel(HITLChannel):
    """Broadcasts HITL approval requests to WebSocket subscribers."""

    def __init__(self):
        self._subscribers: list[Any] = []  # WebSocket connections

    def subscribe(self, ws) -> None:
        """Register a WebSocket connection to receive notifications."""
        self._subscribers.append(ws)

    def unsubscribe(self, ws) -> None:
        self._subscribers = [s for s in self._subscribers if s is not ws]

    async def notify(self, request: ApprovalRequest) -> None:
        message = json.dumps({
            "type": "hitl_approval_request",
            "id": request.id,
            "workflow_id": request.workflow_id,
            "step_name": request.step_name,
            "mode": request.mode.value,
            "payload": request.payload,
            "created_at": request.created_at.isoformat(),
        })

        disconnected = []
        for ws in self._subscribers:
            try:
                await ws.send_text(message)
            except Exception:
                logger.warning("WebSocket send failed, removing subscriber")
                disconnected.append(ws)

        for ws in disconnected:
            self.unsubscribe(ws)

        logger.info(
            "HITL WebSocket notification sent to %d subscribers: %s",
            len(self._subscribers),
            request.id,
        )
