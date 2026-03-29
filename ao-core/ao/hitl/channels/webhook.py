"""Webhook notification channel for HITL approvals.

Sends HTTP POST to a configured URL when an approval is requested.
Useful for integrating with external systems (Teams, Slack, email).
"""

import json
import logging

import httpx

from ao.hitl.manager import ApprovalRequest, HITLChannel

logger = logging.getLogger(__name__)


class WebhookChannel(HITLChannel):
    """Sends HITL approval requests to a webhook URL via HTTP POST."""

    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self._url = url
        self._headers = headers or {"Content-Type": "application/json"}

    async def notify(self, request: ApprovalRequest) -> None:
        payload = {
            "type": "hitl_approval_request",
            "id": request.id,
            "workflow_id": request.workflow_id,
            "step_name": request.step_name,
            "mode": request.mode.value,
            "payload": request.payload,
            "created_at": request.created_at.isoformat(),
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self._url,
                    content=json.dumps(payload),
                    headers=self._headers,
                    timeout=10.0,
                )
                response.raise_for_status()
            logger.info("HITL webhook sent to %s: %s", self._url, request.id)
        except Exception:
            logger.exception("HITL webhook failed for %s to %s", request.id, self._url)
