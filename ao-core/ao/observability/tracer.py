"""OpenTelemetry + Langfuse tracing integration.

Provides a unified tracer that:
- Creates OpenTelemetry spans for distributed tracing
- Sends LLM-specific traces (prompts, completions, tokens, cost) to Langfuse
"""

import logging
from typing import Any

from langfuse import Langfuse
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

logger = logging.getLogger(__name__)


class AOTracer:
    """Unified tracer combining OpenTelemetry spans and Langfuse LLM traces."""

    def __init__(
        self,
        service_name: str = "ao-core",
        langfuse_public_key: str | None = None,
        langfuse_secret_key: str | None = None,
        langfuse_host: str | None = None,
        enable_console_export: bool = False,
    ):
        # OpenTelemetry setup
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        if enable_console_export:
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)
        self._otel_tracer = trace.get_tracer(service_name)

        # Langfuse setup (optional — only if keys provided)
        self._langfuse: Langfuse | None = None
        if langfuse_public_key and langfuse_secret_key:
            self._langfuse = Langfuse(
                public_key=langfuse_public_key,
                secret_key=langfuse_secret_key,
                host=langfuse_host or "http://localhost:3000",
            )

    def start_span(self, name: str, metadata: dict[str, Any] | None = None):
        """Start an OTel span and optionally a Langfuse trace."""
        span = self._otel_tracer.start_span(name)
        ctx = {"otel_span": span}
        if self._langfuse:
            lf_trace = self._langfuse.trace(name=name, metadata=metadata or {})
            ctx["langfuse_trace"] = lf_trace
        return ctx

    def end_span(
        self, ctx: dict, status: str = "completed", error: str | None = None
    ) -> None:
        """End an OTel span and update Langfuse trace."""
        otel_span = ctx.get("otel_span")
        if otel_span:
            if error:
                otel_span.set_attribute("error", True)
                otel_span.set_attribute("error.message", error)
            otel_span.end()

        lf_trace = ctx.get("langfuse_trace")
        if lf_trace:
            lf_trace.update(metadata={"status": status, "error": error})

    def log_llm_call(
        self,
        ctx: dict,
        name: str,
        model: str,
        messages: list[dict[str, str]],
        response: str,
        usage: dict[str, int] | None = None,
    ) -> None:
        """Log an LLM call to Langfuse as a generation."""
        lf_trace = ctx.get("langfuse_trace")
        if lf_trace:
            lf_trace.generation(
                name=name,
                model=model,
                input=messages,
                output=response,
                usage=usage or {},
            )

    def flush(self) -> None:
        """Flush pending traces."""
        if self._langfuse:
            self._langfuse.flush()