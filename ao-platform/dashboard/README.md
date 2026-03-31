# AO Dashboard

Platform dashboard for Agent Orchestration. Shows workflow management, HITL approval queue, policy configuration, and registered DSAI apps.

Served as a single HTML page from the AO Platform API — no build step required.

## Running

Start the AO Platform API (from project root):

```bash
cd ao-platform
uvicorn api.main:app --reload --port 8000
```

Then open **http://localhost:8000/** in your browser.

## Features

- **Overview** — Stat cards and recent workflow runs
- **Workflows** — Register, view, and trigger workflow runs
- **HITL Approvals** — Approve or reject human-in-the-loop requests
- **Policies** — Add and manage guardrail policies per app
- **DSAI Apps** — Quick links to registered applications
