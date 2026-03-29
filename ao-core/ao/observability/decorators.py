"""Tracing decorators — @traced for wrapping functions with OTel spans."""

import functools
import logging
from typing import Any, Callable

from opentelemetry import trace

logger = logging.getLogger(__name__)

_tracer = trace.get_tracer("ao-core")


def traced(name: str | None = None) -> Callable:
    """Decorator that wraps a function in an OpenTelemetry span.

    Usage:
        @traced("my_step")
        async def my_step(state):
            ...
    """

    def decorator(fn: Callable) -> Callable:
        span_name = name or f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with _tracer.start_as_current_span(span_name):
                return await fn(*args, **kwargs)

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with _tracer.start_as_current_span(span_name):
                return fn(*args, **kwargs)

        import asyncio

        if asyncio.iscoroutinefunction(fn):
            return async_wrapper
        return sync_wrapper

    return decorator