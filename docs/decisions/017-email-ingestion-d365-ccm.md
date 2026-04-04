# ADR-017: D365 CCM Email Ingestion

**Status:** Proposed  
**Date:** 2025-07-01

---

## Context

Emails from citizens arrive in the **D365 Customer Case Management (CCM)** portal. Today an officer reads the email, clicks a button in CCM that opens the Tax Email Assistant UI, and submits the case manually.

The objective is to eliminate the manual step: AO should detect new unprocessed emails and trigger the email-drafting workflow automatically as a backend job.

Two questions need answering:

1. **How does AO read new emails from CCM?**
2. **How is the processing state of each email tracked?**

---

## Decision

### Ingestion Pattern — Push via Azure Service Bus

| Option | Latency | Complexity | Notes |
|---|---|---|---|
| **Pull (polling D365 OData API)** | ≥ polling interval | Low | Adds N queries per minute; scales poorly if email volume grows |
| **Push (D365 → Service Bus)** | Near-real-time | Medium | Decoupled; at-least-once delivery; existing infra |

**Recommendation: Push via Azure Service Bus.**

D365 CCM raises an event when a new case email arrives. A **D365 plugin** or **Power Automate cloud flow** writes a lightweight message to an Azure Service Bus queue (`ao-email-ingest`). The AO platform worker subscribes to this queue.

```
D365 CCM (new email event)
  └─ Power Automate / D365 Plugin
       └─ Azure Service Bus → ao-email-ingest queue
            └─ AO Worker (ao-platform/workers/)
                 └─ ManifestExecutor  →  draft-reply workflow
```

The Service Bus message carries the minimum fields needed to fetch the full email:

```json
{
  "email_id": "ccm-email-<guid>",
  "case_number": "CS-2025-001234",
  "subject": "Query on GST registration",
  "received_at": "2025-07-01T08:00:00Z"
}
```

AO worker fetches the full email body from D365 via OData API after receiving the queue message (lazy fetch, not bulk pull).

### State Management

Each received email is recorded in the AO PostgreSQL database:

```sql
CREATE TABLE ao_emails (
    email_id        TEXT PRIMARY KEY,
    case_number     TEXT NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL,
    processing_state TEXT NOT NULL DEFAULT 'unprocessed',
      -- states: unprocessed | in_progress | processed | failed
    workflow_run_id TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Preventing duplicate processing across worker replicas:**

```sql
SELECT * FROM ao_emails
WHERE email_id = $1 AND processing_state = 'unprocessed'
FOR UPDATE SKIP LOCKED;
```

Worker acquires a row-level lock before transitioning to `in_progress`. A competing replica skips the locked row and moves to the next message. On workflow completion, state transitions to `processed`. On uncaught exception, state transitions to `failed`.

#### State transitions

```
unprocessed ──► in_progress ──► processed
                     └──────────► failed ──► (dead-letter queue)
```

Failed emails are forwarded to the Azure Service Bus **dead-letter queue**, which is already consumed by `ao-platform/workers/dead_letter.py` (`DeadLetterProcessor`). The DLQ processor retries up to `max_retries` (default 3) times before alerting.

### Idempotency

Service Bus provides at-least-once delivery. The `SELECT ... FOR UPDATE SKIP LOCKED` guard and the `email_id PRIMARY KEY` constraint together ensure an email is processed exactly once even if the queue delivers the message twice.

---

## Testing — Mocking the Push Pattern

We do not want to depend on a real D365 CCM instance in tests. The push pattern can be mocked at two levels.

### Unit / integration tests — in-process fake queue

`DeadLetterProcessor` already has `enqueue_local()` / `process_batch()` for dev mode. The same approach applies to the `EmailIngestWorker`:

```python
# workers/email_ingest.py (future)
class EmailIngestWorker:
    def enqueue_local(self, msg: EmailIngestMessage) -> None: ...
    async def process_batch(self) -> list[dict]: ...
    async def run_service_bus_consumer(self, queue_name: str) -> None: ...
```

Tests call `enqueue_local()` and `process_batch()` — no Azure SDK, no network.

```python
# tests/integration/test_email_ingest.py
async def test_email_triggers_workflow():
    worker = EmailIngestWorker(executor=mock_executor)
    worker.enqueue_local(EmailIngestMessage(
        email_id="test-001",
        case_number="CS-2025-XYZ",
        subject="GST registration query",
        received_at=datetime.now(timezone.utc),
    ))
    results = await worker.process_batch()
    assert results[0]["state"] == "processed"
```

### End-to-end / staging tests — Azure Service Bus emulator or test queue

For staging validation, two options avoid touching real CCM:

| Option | How |
|---|---|
| **Service Bus emulator** | Microsoft publishes an [Azure Service Bus emulator](https://learn.microsoft.com/azure/service-bus-messaging/overview-emulator) Docker image. Add it to `docker-compose.local.yml`; test publisher writes a message directly to the queue. |
| **Dedicated test queue** | Provision a `ao-email-ingest-test` queue in the dev Service Bus namespace. A small test script (or pytest fixture) acts as the publisher — it sends a synthetic `EmailIngestMessage` JSON blob. The AO worker picks it up from the test queue. |

The Service Bus emulator approach is preferred for local dev because it requires no Azure credentials. The dedicated test queue approach is used in CI (staging environment) where the real Service Bus namespace is available.

### What is NOT mocked

The D365 OData call to fetch the full email body (lazy fetch after receiving the queue message) should be mocked via `unittest.mock.AsyncMock` in unit tests. In staging, the test message's `email_id` is pre-seeded in the `ao_emails` table so the worker can skip the OData fetch and use a fixture payload.

---

## Consequences

- A new **Power Automate flow** (or D365 plugin) must be configured by the CCM team to write to the `ao-email-ingest` Service Bus queue. This is a one-time CCM configuration — no D365 code changes in AO.
- The `ao_emails` table is added to `docker/init.sql` and the Terraform `modules/messaging/` module provisions the new queue.
- AO worker gains a new `EmailIngestWorker` class alongside `DeadLetterProcessor`. The worker loop is a standard Service Bus `receive_messages()` / `complete_message()` pattern.
- Officers retain the ability to use the existing manual UI path until the agentic path is validated in staging.
