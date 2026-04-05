# Debugging ACA Deployments

Practical playbook for diagnosing "the code is right but the deployed app is still wrong" situations on Azure Container Apps.

---

## The Silent Fallback Trap

ACA automatically falls back to the last healthy revision when a new one fails to start. This means:

- `az containerapp revision list` may show the new revision as `active: true, traffic: 100`
- The app still **behaves like the old revision** because the new one crash-looped and ACA quietly re-routed traffic

**Always verify `runningState` before debugging app logic.**

---

## Step-by-Step Diagnostic Flow

### 1. Check whether the new revision is actually running

```powershell
az containerapp revision show `
  --name ca-email-assistant-dev `
  --resource-group rg-ao-dev `
  --revision <revision-name> `
  --query "properties.runningState"
```

Expected values: `Running` (good), `Activating` (still starting), `Failed` / `Degraded` (crash-looped).

If it is not `Running`, skip all other checks and go straight to startup logs.

### 2. Pull startup crash logs

```powershell
az containerapp logs show `
  --name ca-email-assistant-dev `
  --resource-group rg-ao-dev `
  --revision <revision-name> `
  --tail 50
```

> Scale-to-zero hides replicas when idle. Passing `--revision` forces logs from that specific revision even when no replica is running.

Filter for errors quickly:

```powershell
az containerapp logs show ... | Select-String "Error|Exception|Traceback|critical"
```

### 3. Verify the image contains what you think it does

Don't assume the image was built correctly — verify directly:

```powershell
# Check that a specific string is present in a deployed file
docker run --rm <acr-login-server>/<image>:<tag> `
  grep -n "keyword_to_find" /app/path/to/file.py
```

Pull the image first if needed:

```powershell
az acr login --name <acr-name>
docker pull <acr-login-server>/<image>:<tag>
```

### 4. Confirm which revision is actually serving traffic

```powershell
az containerapp revision list `
  --name ca-email-assistant-dev `
  --resource-group rg-ao-dev `
  --query "[?properties.active].{name:name, traffic:properties.trafficWeight, state:properties.runningState}" `
  -o table
```

`traffic:100` + `state:Running` together confirm the revision is live.

---

## Known Root Causes (with fixes)

### AsyncRedisSaver crash at import

**Symptom:** New revision shows `Failed`/`Degraded`; logs show `TypeError` or `AttributeError` on AsyncRedisSaver during startup.

**Root cause:** `AsyncRedisSaver.from_conn_string()` is decorated with `@asynccontextmanager` — it returns a generator, not a `BaseCheckpointSaver`. LangGraph rejects the generator at graph-build time → unhandled exception at module import → container exits → ACA falls back to previous healthy revision.

**Fix (2026-04):** AsyncRedisSaver removed entirely. Azure Basic/Standard Redis tiers do not include the RedisJSON module required by `AsyncRedisSaver`. Use `MemorySaver` for LangGraph checkpointing; persist only email *state* (not graph checkpoints) to Redis via `ShortTermMemory.set_data()`.

---

### Workers > 1 breaks in-process cancel/stop

**Symptom:** Stop button always returns 404 ("No active stream for this email") even when a stream is visibly running.

**Root cause:** `uvicorn --workers 2` spawns two OS processes. The `_active_streams` dict lives in process memory. The cancel request is routed to the *other* process, which has no entry for that email.

**Fix:** Set `--workers 1` in `Dockerfile.email-assistant`. ACA horizontal scaling handles concurrency at the replica level, not via uvicorn workers.

---

### Redis connection string format

**Symptom:** `Redis unavailable` warning in logs; email state does not persist across restarts.

**Root cause:** The Azure portal "Primary connection string" format (`<host>:6380,password=<key>,...`) is not a valid `redis://` URL. The `redis.asyncio` client expects a URL scheme.

**Fix:** Build the URL in Terraform output:

```hcl
output "redis_connection_string" {
  value = "rediss://:${azurerm_redis_cache.main.primary_access_key}@${azurerm_redis_cache.main.hostname}:${azurerm_redis_cache.main.ssl_port}"
}
```

Note `rediss://` (double-s) for TLS, which Azure Redis requires on port 6380.

---

### GeneratorExit leaves emails stuck in "processing"

**Symptom:** Refreshing the page shows an email frozen on "Initializing..." indefinitely. The cancel endpoint returns 404 (no active stream). The email cannot be re-processed.

**Root cause:** When a browser closes the SSE connection mid-stream, Python raises `GeneratorExit` inside the async generator. `GeneratorExit` is a `BaseException`, not an `Exception`, so it bypasses `except Exception` blocks. The `finally` block runs, but if it only removes the stream from `_active_streams` without checking `email["status"]`, the email stays in `"processing"` forever.

**Fix:** Check and reset status in the `finally` block:

```python
finally:
    _active_streams.pop(email_id, None)
    if email.get("status") == "processing":
        email["status"] = "interrupted"
        await _persist_email_state(email_id, email)
```

**Recovery (for emails already stuck):** `POST /api/emails/{id}/reset` resets a single stuck email. `POST /api/emails/reset-all` clears all email state from both memory and Redis.

---

### Supervisor calling the same specialist multiple times

**Symptom:** Logs show the same specialist agent (`payment_arrangement`, etc.) invoked 3+ times in a single run. LLM ignores the "do not repeat" instruction in the prompt.

**Root cause:** LLMs do not reliably follow negative constraints in system prompts, especially under token pressure.

**Fix:** Enforce no-repeat in code, not in the prompt:

```python
# In _make_supervisor_node, after getting the LLM decision:
if decision in specialist_outputs:
    logger.warning("Supervisor tried to repeat %s — forcing FINISH", decision)
    decision = "finish"
```

---

## Useful One-Liners

```powershell
# List all revisions with traffic and run state
az containerapp revision list `
  --name ca-email-assistant-dev --resource-group rg-ao-dev `
  --query "[].{name:name,traffic:properties.trafficWeight,state:properties.runningState,active:properties.active}" `
  -o table

# Restart a specific revision (clears in-memory state)
az containerapp revision restart `
  --name ca-email-assistant-dev --resource-group rg-ao-dev `
  --revision <revision-name>

# Tail logs for a specific revision (works even at zero scale)
az containerapp logs show `
  --name ca-email-assistant-dev --resource-group rg-ao-dev `
  --revision <revision-name> --tail 50

# Verify image content without a full docker pull
docker run --rm <acr>/<image>:<tag> grep -rn "search_term" /app/

# Test the SSE stream from PowerShell
Invoke-WebRequest -Uri "https://<host>/api/emails/<id>/process/stream" -Method GET

# Cancel a stream
Invoke-RestMethod -Uri "https://<host>/api/emails/<id>/cancel" -Method POST

# Reset a stuck email
Invoke-RestMethod -Uri "https://<host>/api/emails/<id>/reset" -Method POST
```

---

## Check All App Health States (Snapshot)

Run this command to get a quick health overview of every Container App at once:

```powershell
# List all apps with their latest revision and provisioning state
az containerapp list `
  --resource-group rg-ao-dev `
  --subscription 78205397-1833-43c4-977e-d177b245a3ad `
  --query "[].{name:name, latestRevision:properties.latestRevisionName, provisioningState:properties.provisioningState}" `
  -o table
```

Then verify the health state of each latest revision individually. Replace `<app-name>` and `<revision-name>` from the table above:

```powershell
az containerapp revision show `
  --name <app-name> `
  --resource-group rg-ao-dev `
  --revision <revision-name> `
  --query "{state:properties.healthState, active:properties.active, replicas:properties.replicas}" `
  -o json
```

Expected output when healthy:
```json
{
  "active": true,
  "replicas": 1,
  "state": "Healthy"
}
```

> If `state` is `Unhealthy`, check startup logs for the revision using step 2 in the Diagnostic Flow above. Common cause: missing `__main__.py` entrypoint (worker apps), import errors, or failing health probes.
