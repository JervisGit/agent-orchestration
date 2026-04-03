"""AppRuntime — standardised factory for DSAI apps integrating with AO.

Every app that connects to AO should use this factory instead of manually
constructing LLM providers, Langfuse clients, and policy sets.  It reads
environment variables once, builds the ManifestExecutor, and exposes helpers
for the two most common post-run operations: loading policies and persisting
HITL requests.

Usage::

    from ao.runtime import AppRuntime

    runtime = AppRuntime.from_env(MANIFEST_PATH)
    runtime.executor.register_tool("my_tool", my_tool_fn, MY_TOOL_SCHEMA)
    compiled_graph = runtime.executor.compile(state_schema=MyState)

    # After a run, in your SSE/HTTP handler:
    hitl_id = await runtime.maybe_persist_hitl(
        item={"id": item_id, "sender": from_addr, "subject": subject},
        final_state=final_state,
        trace_id=trace_id,
        action_webhook_template="http://localhost:8001/api/hitl/{}/execute",
    )
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

from ao.config.manifest import AppManifest
from ao.engine.manifest_executor import ManifestExecutor
from ao.llm.base import LLMProvider
from ao.policy.schema import PolicySet

logger = logging.getLogger(__name__)


# ── LLM factory (used by AppRuntime.from_env) ────────────────────────

def build_llm() -> LLMProvider:
    """Create the best LLM provider available from environment variables.

    Resolution order: OpenAI → Azure OpenAI → Ollama.
    Raises RuntimeError if none are configured.
    """
    if os.getenv("OPENAI_API_KEY"):
        from ao.llm.openai import OpenAIProvider
        return OpenAIProvider(
            api_key=os.environ["OPENAI_API_KEY"],
            default_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        )
    if os.getenv("AZURE_OPENAI_ENDPOINT"):
        from ao.llm.azure_openai import AzureOpenAIProvider
        return AzureOpenAIProvider(
            endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
        )
    if os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_MODEL"):
        from ao.llm.ollama import OllamaProvider
        return OllamaProvider(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            default_model=os.getenv("OLLAMA_MODEL", "gemma3:1b"),
        )
    raise RuntimeError(
        "No LLM configured. Set OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, or OLLAMA_BASE_URL."
    )


# ── Langfuse factory ─────────────────────────────────────────────────

def build_langfuse() -> Any | None:
    """Create a Langfuse client from environment variables, or None if not configured."""
    pk = os.getenv("LANGFUSE_PUBLIC_KEY")
    sk = os.getenv("LANGFUSE_SECRET_KEY")
    if not (pk and sk):
        return None
    try:
        from langfuse import Langfuse
        return Langfuse(
            public_key=pk,
            secret_key=sk,
            host=os.getenv("LANGFUSE_HOST", "http://localhost:3000"),
        )
    except ImportError:
        logger.warning("langfuse package not installed — tracing disabled")
        return None


# ── AppRuntime ────────────────────────────────────────────────────────

class AppRuntime:
    """All wiring an AO-integrated app needs in one object.

    Attributes
    ----------
    executor     : ManifestExecutor — ready for register_tool() then compile()
    manifest     : AppManifest — parsed from the YAML file
    llm          : LLMProvider — exposed so apps can use it outside the executor
    platform_url : base URL of the AO Platform API
    """

    def __init__(
        self,
        executor: ManifestExecutor,
        manifest: AppManifest,
        llm: LLMProvider,
        platform_url: str,
        langfuse_client: Any | None = None,
    ) -> None:
        self.executor = executor
        self.manifest = manifest
        self.llm = llm
        self.platform_url = platform_url.rstrip("/")
        self.langfuse_client = langfuse_client

    # ── Factory ──────────────────────────────────────────────────────

    @classmethod
    def from_env(
        cls,
        manifest_path: str | Path,
        *,
        env_file: Path | None = None,
    ) -> "AppRuntime":
        """Build an AppRuntime from environment variables.

        Parameters
        ----------
        manifest_path : path to the app's ``ao-manifest.yaml``
        env_file      : optional ``.env`` file to load before reading vars
                        (useful for local dev; skipped silently if dotenv is
                        not installed or the file does not exist)
        """
        if env_file is not None:
            try:
                from dotenv import load_dotenv
                load_dotenv(env_file, override=True)
            except (ImportError, OSError):
                pass

        llm = build_llm()
        langfuse = build_langfuse()
        manifest = AppManifest.from_yaml(Path(manifest_path))
        executor = ManifestExecutor(manifest, llm=llm, langfuse_client=langfuse)
        platform_url = os.getenv("AO_PLATFORM_URL", "http://localhost:8000")

        logger.info(
            "AppRuntime initialised  app_id=%s  pattern=%s  tracing=%s",
            manifest.app_id,
            manifest.pattern,
            "enabled" if langfuse else "disabled",
        )
        return cls(
            executor=executor,
            manifest=manifest,
            llm=llm,
            platform_url=platform_url,
            langfuse_client=langfuse,
        )

    # ── Policy loading ────────────────────────────────────────────────

    async def load_policies(self) -> PolicySet | None:
        """Fetch active policies for this app from the AO Platform API.

        Returns ``None`` on any failure; callers should fall back to
        manifest-inline policies in that case.
        """
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    f"{self.platform_url}/api/policies/",
                    params={"app_id": self.manifest.app_id},
                )
                resp.raise_for_status()
                pols = (resp.json() or {}).get("policies", [])
                if not pols:
                    return None
                yaml_lines = ["policies:"]
                for p in pols:
                    yaml_lines.append(f"  - name: {p['name']}")
                    yaml_lines.append(f"    stage: {p['stage']}")
                    yaml_lines.append(f"    action: {p['action']}")
                return PolicySet.from_yaml("\n".join(yaml_lines))
        except Exception as exc:
            logger.warning("Could not load policies from AO Platform: %s", exc)
            return None

    # ── HITL persistence ──────────────────────────────────────────────

    async def maybe_persist_hitl(
        self,
        item: dict,
        final_state: dict,
        trace_id: str,
        action_webhook_template: str = "",
    ) -> str | None:
        """Persist a HITL request to the AO Platform if ``hitl_required`` is True.

        Parameters
        ----------
        item : dict
            Identity of the triggering item — at minimum ``id``, ``sender``,
            ``subject`` (or ``title``).  Any extra keys are stored verbatim
            in the platform payload.
        final_state : dict
            Final LangGraph state after the run.  Expected keys:
            ``hitl_required`` (bool), ``hitl_action`` (str), ``output`` (str).
        trace_id : str
            Langfuse / internal trace ID for this run.
        action_webhook_template : str
            URL template with a single ``{}`` placeholder that will be filled
            with the new ``request_id``.
            Example: ``"http://localhost:8001/api/hitl/{}/execute"``

        Returns
        -------
        str | None
            The new ``request_id`` (UUID string) if persisted, ``None``
            if HITL is not required or persistence failed.
        """
        if not final_state.get("hitl_required"):
            return None

        import json as _json
        import uuid

        request_id = str(uuid.uuid4())

        # Resolve {key} and {dict_key} placeholders in hitl_action text
        hitl_action_text: str = final_state.get("hitl_action", "Review decision")
        for k, v in final_state.items():
            if isinstance(v, str):
                hitl_action_text = hitl_action_text.replace(f"{{{k}}}", v)
            elif isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    hitl_action_text = hitl_action_text.replace(
                        f"{{{k}_{sub_k}}}", str(sub_v) if sub_v is not None else ""
                    )

        # Extract triggering agent name from policy_flags
        step_name = "unknown"
        for flag in final_state.get("policy_flags", []):
            if "HITL_REQUIRED: agent=" in flag:
                try:
                    step_name = flag.split("agent=")[1].split(" ")[0]
                except IndexError:
                    pass
                break

        payload: dict = {
            **item,
            "proposed_action": hitl_action_text,
            "draft_output": final_state.get("output", ""),
            "trace_id": trace_id,
            "app_id": self.manifest.app_id,
        }
        if action_webhook_template:
            payload["action_webhook"] = action_webhook_template.format(request_id)

        # Try platform API first
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self.platform_url}/api/hitl/requests",
                    json={
                        "request_id": request_id,
                        "workflow_id": trace_id,
                        "step_name": step_name,
                        "payload": payload,
                    },
                )
                resp.raise_for_status()
            logger.info(
                "HITL request %s created via platform  app=%s  trace=%s",
                request_id, self.manifest.app_id, trace_id,
            )
            return request_id
        except Exception as exc:
            logger.warning("Platform HITL API unavailable (%s) — falling back to direct DB", exc)
            return await self._persist_hitl_direct(request_id, trace_id, step_name, payload)

    async def _persist_hitl_direct(
        self,
        request_id: str,
        trace_id: str,
        step_name: str,
        payload: dict,
    ) -> str | None:
        """Write HITL request directly to the DB when the Platform API is unreachable."""
        import json as _json

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            logger.warning(
                "HITL persistence skipped: platform API unavailable and no DATABASE_URL set"
            )
            return None
        try:
            import psycopg

            async with await psycopg.AsyncConnection.connect(database_url) as conn:
                await conn.execute(
                    "INSERT INTO ao_hitl_requests"
                    " (request_id, workflow_id, step_name, status, payload)"
                    " VALUES (%s, %s, %s, 'pending', %s::jsonb)",
                    (request_id, trace_id, step_name, _json.dumps(payload)),
                )
                await conn.commit()
            logger.info(
                "HITL request %s persisted directly to DB (platform fallback)", request_id
            )
            return request_id
        except Exception:
            logger.exception("Failed to persist HITL request for trace %s", trace_id)
            return None
