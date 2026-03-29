"""HITL approval flow orchestration.

Manages human-in-the-loop approval requests:
- Creates approval requests when a workflow step requires human review
- Sends notifications via configured channels (WebSocket, webhook)
- Blocks workflow execution until approval/rejection or timeout
- Supports per-environment toggle (auto-approve in dev, require in prod)
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ApprovalMode(Enum):
    REQUIRED = "required"    # Must be approved by a human
    OPTIONAL = "optional"    # Human can review, but auto-approves on timeout
    AUTO = "auto"            # Skip HITL entirely (e.g., dev environment)


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"


@dataclass
class ApprovalRequest:
    """A request for human approval of a workflow step."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str = ""
    step_name: str = ""
    mode: ApprovalMode = ApprovalMode.REQUIRED
    status: ApprovalStatus = ApprovalStatus.PENDING
    payload: dict[str, Any] = field(default_factory=dict)  # Context for the reviewer
    reviewer: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    resolution_note: str = ""


class HITLChannel:
    """Base class for notification channels."""

    async def notify(self, request: ApprovalRequest) -> None:
        """Send a notification about a pending approval."""
        raise NotImplementedError


class HITLManager:
    """Orchestrates human-in-the-loop approval flows."""

    def __init__(
        self,
        default_mode: ApprovalMode = ApprovalMode.REQUIRED,
        timeout_seconds: float = 300.0,  # 5 minutes default
        channels: list[HITLChannel] | None = None,
    ):
        self._default_mode = default_mode
        self._timeout = timeout_seconds
        self._channels = channels or []
        self._pending: dict[str, ApprovalRequest] = {}
        self._events: dict[str, asyncio.Event] = {}

    @property
    def pending_requests(self) -> list[ApprovalRequest]:
        return [r for r in self._pending.values() if r.status == ApprovalStatus.PENDING]

    def add_channel(self, channel: HITLChannel) -> None:
        self._channels.append(channel)

    async def request_approval(
        self,
        workflow_id: str,
        step_name: str,
        payload: dict[str, Any] | None = None,
        mode: ApprovalMode | None = None,
        timeout: float | None = None,
    ) -> ApprovalRequest:
        """Create an approval request and wait for resolution.

        Args:
            workflow_id: The workflow requesting approval.
            step_name: The step that needs approval.
            payload: Context data for the reviewer (e.g., draft output to review).
            mode: Override approval mode for this request.
            timeout: Override timeout in seconds.

        Returns:
            The resolved ApprovalRequest.
        """
        effective_mode = mode or self._default_mode
        effective_timeout = timeout or self._timeout

        # Auto mode: skip entirely
        if effective_mode == ApprovalMode.AUTO:
            logger.info("HITL auto-approved: %s/%s", workflow_id, step_name)
            return ApprovalRequest(
                workflow_id=workflow_id,
                step_name=step_name,
                mode=effective_mode,
                status=ApprovalStatus.APPROVED,
                payload=payload or {},
                resolved_at=datetime.now(timezone.utc),
                resolution_note="Auto-approved (HITL mode=auto)",
            )

        # Create pending request
        request = ApprovalRequest(
            workflow_id=workflow_id,
            step_name=step_name,
            mode=effective_mode,
            payload=payload or {},
        )
        self._pending[request.id] = request
        event = asyncio.Event()
        self._events[request.id] = event

        logger.info(
            "HITL approval requested: %s (workflow=%s, step=%s)",
            request.id,
            workflow_id,
            step_name,
        )

        # Notify channels
        for channel in self._channels:
            try:
                await channel.notify(request)
            except Exception:
                logger.exception("Failed to notify channel %s", type(channel).__name__)

        # Wait for resolution or timeout
        try:
            await asyncio.wait_for(event.wait(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            if effective_mode == ApprovalMode.OPTIONAL:
                request.status = ApprovalStatus.APPROVED
                request.resolution_note = "Auto-approved on timeout (mode=optional)"
                logger.info("HITL optional timeout, auto-approved: %s", request.id)
            else:
                request.status = ApprovalStatus.TIMED_OUT
                logger.warning("HITL timed out: %s", request.id)
            request.resolved_at = datetime.now(timezone.utc)

        # Cleanup
        self._events.pop(request.id, None)
        return request

    def resolve(
        self,
        request_id: str,
        approved: bool,
        reviewer: str | None = None,
        note: str = "",
    ) -> ApprovalRequest | None:
        """Resolve a pending approval request (called by API/dashboard/webhook).

        Args:
            request_id: The approval request ID.
            approved: True to approve, False to reject.
            reviewer: Who resolved it.
            note: Optional note from the reviewer.

        Returns:
            The updated ApprovalRequest, or None if not found.
        """
        request = self._pending.get(request_id)
        if not request or request.status != ApprovalStatus.PENDING:
            return None

        request.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        request.reviewer = reviewer
        request.resolution_note = note
        request.resolved_at = datetime.now(timezone.utc)

        event = self._events.get(request_id)
        if event:
            event.set()

        logger.info(
            "HITL %s: %s (reviewer=%s)",
            request.status.value,
            request_id,
            reviewer,
        )
        return request
