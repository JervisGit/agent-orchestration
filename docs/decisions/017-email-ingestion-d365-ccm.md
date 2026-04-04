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

## Consequences

- A new **Power Automate flow** (or D365 plugin) must be configured by the CCM team to write to the `ao-email-ingest` Service Bus queue. This is a one-time CCM configuration — no D365 code changes in AO.
- The `ao_emails` table is added to `docker/init.sql` and the Terraform `modules/messaging/` module provisions the new queue.
- AO worker gains a new `EmailIngestWorker` class alongside `DeadLetterProcessor`. The worker loop is a standard Service Bus `receive_messages()` / `complete_message()` pattern.
- Officers retain the ability to use the existing manual UI path until the agentic path is validated in staging.
