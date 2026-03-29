# Local Development Guide

How to spin up the full AO stack + DSAI app demos locally for end-to-end testing.

---

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | 3.11+ | AO SDK + app backends |
| Docker & Docker Compose | Latest | Redis, PostgreSQL, Langfuse |
| Ollama | Latest | Local LLM inference |
| Node.js | 18+ | Dashboard / app frontends (future) |
| Git | Latest | Version control |

---

## 1. Clone & Set Up Python Environment

```bash
git clone <repo-url> agent-orchestration
cd agent-orchestration

python -m venv .venv
# Windows
.\.venv\Scripts\Activate.ps1
# macOS/Linux
source .venv/bin/activate

pip install -e ao-core[dev]
pip install fastapi uvicorn deepeval
```

---

## 2. Start Infrastructure Services

These provide the real backing services (Redis, PostgreSQL+pgvector, Langfuse) that the AO SDK connects to.

```bash
docker compose -f docker/docker-compose.local.yml up -d redis postgres langfuse
```

| Service | URL | Credentials |
|---|---|---|
| Redis | `redis://localhost:6379` | None (local dev) |
| PostgreSQL | `postgresql://ao:localdev@localhost:5432/ao` | User: `ao` / Pass: `localdev` |
| Langfuse | `http://localhost:3000` | Create account on first visit |

### Verify services are running

```bash
docker compose -f docker/docker-compose.local.yml ps
```

### Langfuse setup (first time)

1. Open `http://localhost:3000` and create an account
2. Create a project for each app: `email-assistant`, `rag-search`, `graph-compliance`
3. Note the **public key** and **secret key** for each project
4. Set environment variables:
   ```bash
   export LANGFUSE_HOST=http://localhost:3000
   export LANGFUSE_PUBLIC_KEY=pk-lf-...
   export LANGFUSE_SECRET_KEY=sk-lf-...
   ```

---

## 3. Start Ollama (Local LLM)

If Ollama is not already running, start it and pull a model:

```bash
ollama serve                       # Start the server (if not running as service)
ollama pull gemma3:1b              # Small & fast for testing
ollama pull gemma:2b               # Alternative
```

Verify:
```bash
curl http://localhost:11434/api/tags
```

### Environment variable (optional)

The demos default to `gemma3:1b`. Override with:
```bash
export OLLAMA_MODEL=gemma:2b       # Or any model you've pulled
export OLLAMA_BASE_URL=http://localhost:11434
```

---

## 4. Run AO Platform API

```bash
cd ao-platform
uvicorn api.main:app --reload --port 8000
```

Verify:
```bash
curl http://localhost:8000/health
# → {"status":"ok"}
```

API endpoints:
- `GET  /api/workflows/` — list workflows
- `POST /api/workflows/{id}/run` — run a workflow
- `GET  /api/hitl/pending` — list pending HITL approvals
- `POST /api/hitl/{id}/resolve` — approve/reject
- `GET  /api/policies/` — list policies
- `POST /api/policies/` — create policy

---

## 5. Run Demo Scripts

### Email Assistant — Mock LLM (no Ollama needed)
```bash
python examples/email_assistant/backend/demo.py
```

### Email Assistant — Real LLM (requires Ollama)
```bash
python examples/email_assistant/backend/demo_llm.py
```

### Phase 2 Demo — HITL + Resilience (no Ollama needed)
```bash
python examples/email_assistant/backend/demo_phase2.py
```

### Phase 3 Demo — Advanced Patterns (no Ollama needed)
```bash
python examples/email_assistant/backend/demo_phase3.py
```

---

## 6. Run Tests

```bash
# All tests (78 tests)
python -m pytest tests/ -v

# By category
python -m pytest tests/unit/ -v          # 51 tests — fast, no services needed
python -m pytest tests/integration/ -v   # 7 tests — no external services needed
python -m pytest tests/eval/ -v          # 7 tests — mock LLM eval
python -m pytest tests/security/ -v      # 13 tests — red-team policy checks

# With DeepEval (once integrated)
deepeval test run tests/eval/
```

---

## 7. DSAI App Development (Example Workflow)

Each DSAI app lives in `examples/<app>/` during development, but in production lives in its own repo.

### Creating a new app workflow

1. **Define the manifest** — create `ao-manifest.yaml`:
   ```yaml
   app_id: my_new_app
   display_name: My New App
   identity_mode: service
   agents:
     - name: my_agent
       model: gemma3:1b       # Or gpt-4o for Azure OpenAI
       tools: []
   policies:
     - name: content_safety
       stage: post_execution
       action: warn
   ```

2. **Write the workflow** — use AO patterns:
   ```python
   from ao.engine.patterns.linear import build_linear_chain
   from ao.llm.ollama import OllamaProvider

   llm = OllamaProvider()

   async def my_step(state):
       resp = await llm.complete([
           {"role": "system", "content": "You are helpful."},
           {"role": "user", "content": state["input"]},
       ])
       return {"output": resp.content}

   graph = build_linear_chain([("my_step", my_step)])
   ```

3. **Run it**:
   ```bash
   python examples/my_new_app/backend/demo.py
   ```

---

## Service Ports Summary

| Service | Port | URL |
|---|---|---|
| AO Platform API | 8000 | `http://localhost:8000` |
| Langfuse | 3000 | `http://localhost:3000` |
| PostgreSQL | 5432 | `postgresql://ao:localdev@localhost:5432/ao` |
| Redis | 6379 | `redis://localhost:6379` |
| Ollama | 11434 | `http://localhost:11434` |

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `docker compose` fails | Ensure Docker Desktop is running |
| Langfuse won't start | Check PostgreSQL is healthy: `docker logs <postgres-container>` |
| Ollama connection refused | Run `ollama serve` or check it's running as a service |
| `ModuleNotFoundError: ao` | Run `pip install -e ao-core` from the repo root |
| Tests fail with `DeprecationWarning` | Harmless — asyncio event loop warning, tests still pass |
| Port conflict | Change ports in `docker-compose.local.yml` or stop conflicting services |

---

## Stopping Everything

```bash
# Stop infrastructure
docker compose -f docker/docker-compose.local.yml down

# Stop with data cleanup (removes volumes)
docker compose -f docker/docker-compose.local.yml down -v

# Deactivate venv
deactivate
```
