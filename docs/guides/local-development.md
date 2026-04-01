# Local Development Guide

How to spin up the full AO stack + Email Assistant demo locally.

---

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | **3.13** | AO SDK + app backends |
| Docker Desktop | Latest | PostgreSQL, Redis, Langfuse |
| Ollama | Latest | Local LLM inference (optional if using OpenAI) |
| Git | Latest | Version control |

---

## 1. Clone & Set Up Python Environment

```powershell
git clone <repo-url> agent-orchestration
cd agent-orchestration

python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Windows PowerShell
# source .venv/bin/activate            # macOS/Linux

# Install AO SDK + all runtime deps (fastapi, psycopg, langfuse, etc.)
pip install -e ao-core
pip install fastapi "uvicorn[standard]" python-dotenv python-multipart python-json-logger deepeval
```

---

## 2. Configure Environment Variables

```powershell
Copy-Item .env.example .env
```

Then edit `.env` and **un-comment one LLM block**:

- **OpenAI** (quickest): set `OPENAI_API_KEY`
- **Azure OpenAI**: set `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_API_KEY`
- **Ollama** (no cost, already uncommented as default): pull a model first — see step 3

`DATABASE_URL` and `LANGFUSE_HOST` are pre-filled for the local Docker services and don't need changes.

---

## 3. Start Infrastructure Services

```powershell
docker compose -f docker/docker-compose.local.yml up -d redis postgres langfuse
```

| Service | URL | Credentials |
|---|---|---|
| PostgreSQL | `postgresql://ao:localdev@localhost:5432/ao` | `ao` / `localdev` |
| Redis | `redis://localhost:6379` | — |
| Langfuse | `http://localhost:3000` | Create account on first visit |

Wait ~10 seconds for Postgres to initialise (`init.sql` runs automatically).

```powershell
# Verify all three are healthy
docker compose -f docker/docker-compose.local.yml ps
```

### Langfuse first-time setup

1. Open `http://localhost:3000` — create a local account (any email/password)
2. Create a project named `email-assistant`
3. Copy the **Public Key** and **Secret Key** into `.env`:
   ```
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   ```
   Tracing is optional — the apps work without it.

---

## 4. Pull an Ollama Model (if using Ollama)

```powershell
ollama serve                   # start if not running as a service
ollama pull gemma3:1b          # ~800 MB, fast for testing
```

Verify:
```powershell
curl http://localhost:11434/api/tags
```

> To use OpenAI or Azure OpenAI instead, leave `OLLAMA_*` vars commented out and set the relevant key in `.env`.

---

## 5. Run the AO Platform API (port 8000)

```powershell
# From repo root — PYTHONPATH is set automatically by the --app-dir flag
.venv\Scripts\python -m uvicorn api.main:app --reload --port 8000 --app-dir ao-platform
```

Verify:
```powershell
curl http://localhost:8000/healthz
# → {"status":"ok","checks":{"db":"ok"}}
```

Available endpoints:
| Method | Path | Description |
|---|---|---|
| GET | `/` | Dashboard UI |
| GET | `/healthz` | Liveness/readiness check |
| GET | `/api/hitl/pending` | HITL approval queue |
| POST | `/api/hitl/{id}/resolve` | Approve or reject a HITL request |
| GET | `/api/workflows/` | List registered workflows |
| GET | `/api/policies/` | List active policies |

---

## 6. Run the Email Assistant (port 8001)

Open a second terminal:

```powershell
.venv\Scripts\python -m uvicorn backend.app:app --reload --port 8001 --app-dir examples/email_assistant
```

Verify:
```powershell
curl http://localhost:8001/healthz
# → {"status":"ok","checks":{"db":"ok","llm":"ok"}}
```

Then open **`http://localhost:8001`** in a browser.

### Sample emails

| ID | Scenario | Pattern |
|---|---|---|
| em-001 – em-005 | Single-intent: filing extension, payment, relief, waiver, general | `concurrent` (1 specialist) |
| em-006 | Penalty waiver with 3 prior penalties → **HITL flagged** | `concurrent` + HITL |
| em-007 | Multi-intent: filing extension **and** payment arrangement | `concurrent` (2 specialists + LLM merge) |

To approve a HITL request raised by em-006, go to the **AO Platform Dashboard** at `http://localhost:8000` and use the HITL queue, or use the **Approve Action** button on the email detail page in the email assistant UI.

---

## 7. Run Tests

```powershell
# All tests
python -m pytest tests/ -v

# By category
python -m pytest tests/unit/ -v          # ~51 tests — no services needed
python -m pytest tests/integration/ -v   # ~7  tests — no external services needed
python -m pytest tests/eval/ -v          # ~7  tests — mock LLM
python -m pytest tests/security/ -v      # ~13 tests — policy red-team checks

# With DeepEval
deepeval test run tests/eval/
```

---

## 8. Run Demo Scripts (optional)

These are standalone scripts that exercise the AO SDK without the FastAPI server:

```powershell
# Set PYTHONPATH so ao-core is importable
$env:PYTHONPATH = "ao-core"

python examples/email_assistant/backend/demo.py           # Mock LLM
python examples/email_assistant/backend/demo_llm.py       # Real LLM via Ollama/OpenAI
python examples/email_assistant/backend/demo_phase2.py    # HITL + resilience
python examples/email_assistant/backend/demo_phase3.py    # Advanced patterns
```

---

## 9. Run with Docker Compose (all services)

To run the full stack including the Python apps in containers (mirrors production):

```powershell
# Build and start everything
docker compose -f docker/docker-compose.local.yml up --build

# Or start infrastructure only and run Python apps natively (faster for dev)
docker compose -f docker/docker-compose.local.yml up -d redis postgres langfuse
```

Pass LLM keys via the `.env` file — Docker Compose picks it up automatically:
```powershell
# The compose file reads OPENAI_API_KEY from your shell environment
$env:OPENAI_API_KEY = "sk-..."
docker compose -f docker/docker-compose.local.yml up --build
```

---

## Service Ports Summary

| Service | Port | URL |
|---|---|---|
| Email Assistant | 8001 | `http://localhost:8001` |
| AO Platform API + Dashboard | 8000 | `http://localhost:8000` |
| Langfuse | 3000 | `http://localhost:3000` |
| PostgreSQL | 5432 | `postgresql://ao:localdev@localhost:5432/ao` |
| Redis | 6379 | `redis://localhost:6379` |
| Ollama | 11434 | `http://localhost:11434` |

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `ModuleNotFoundError: ao` | Run `pip install -e ao-core` from repo root |
| `ModuleNotFoundError: dotenv` | Run `pip install python-dotenv` |
| PostgreSQL connection refused | Run `docker compose -f docker/docker-compose.local.yml up -d postgres` |
| Langfuse won't start | Wait for Postgres to be healthy: `docker logs <postgres-container>` |
| Ollama connection refused | Run `ollama serve` or check it's running as a Windows service |
| `/healthz` returns `"llm":"error"` | Check your `.env` — LLM vars not set or Ollama not running |
| Port conflict | Change ports in `docker/docker-compose.local.yml` or stop conflicting services |
| `docker compose` not found | Use `docker-compose` (v1) or update Docker Desktop |

---

## Stopping Everything

```powershell
# Stop infrastructure containers
docker compose -f docker/docker-compose.local.yml down

# Stop and remove volumes (full reset — drops all DB data)
docker compose -f docker/docker-compose.local.yml down -v

# Deactivate venv
deactivate
```

