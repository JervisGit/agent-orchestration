"""Short-term memory — Redis-backed session/conversation memory.

Stores per-session conversation history and scratchpad data with TTL.
"""

import json
import logging
from typing import Any

import redis.asyncio as redis

logger = logging.getLogger(__name__)

DEFAULT_TTL = 3600  # 1 hour


class ShortTermMemory:
    """Redis-backed short-term memory for conversation context."""

    def __init__(self, redis_url: str = "redis://localhost:6379", ttl: int = DEFAULT_TTL):
        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl

    def _key(self, session_id: str, namespace: str = "messages") -> str:
        return f"ao:memory:{session_id}:{namespace}"

    async def add_message(self, session_id: str, role: str, content: str) -> None:
        """Append a message to the conversation history."""
        key = self._key(session_id)
        msg = json.dumps({"role": role, "content": content})
        await self._redis.rpush(key, msg)
        await self._redis.expire(key, self._ttl)

    async def get_messages(self, session_id: str, limit: int = 50) -> list[dict[str, str]]:
        """Retrieve recent conversation messages."""
        key = self._key(session_id)
        raw = await self._redis.lrange(key, -limit, -1)
        return [json.loads(m) for m in raw]

    async def set_data(self, session_id: str, key: str, value: Any) -> None:
        """Store arbitrary session data."""
        rkey = self._key(session_id, namespace="data")
        await self._redis.hset(rkey, key, json.dumps(value))
        await self._redis.expire(rkey, self._ttl)

    async def get_data(self, session_id: str, key: str) -> Any:
        """Retrieve session data by key."""
        rkey = self._key(session_id, namespace="data")
        raw = await self._redis.hget(rkey, key)
        return json.loads(raw) if raw else None

    async def clear_session(self, session_id: str) -> None:
        """Delete all data for a session."""
        keys = [self._key(session_id, ns) for ns in ("messages", "data")]
        await self._redis.delete(*keys)

    async def close(self) -> None:
        await self._redis.aclose()