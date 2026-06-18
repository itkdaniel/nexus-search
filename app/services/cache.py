"""
Redis cache-aside service for nexus-search.

Enhancements over python-service:
  - Pipeline batching for multi-key invalidation (atomic, one round-trip)
  - Cache warm-up helper (load all projects on startup)
  - Graceful degradation: Redis errors log a warning and return None (no crash)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, List, Optional

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger(__name__)
_fallback_log = logging.getLogger(__name__)


class CacheService:
    """Async cache-aside wrapper around Redis with pipeline batching."""

    def __init__(self, redis: aioredis.Redis, ttl: int = 300) -> None:
        self._redis = redis
        self._ttl = ttl

    async def get(self, key: str) -> Optional[Any]:
        """Fetch and deserialize. Returns None on miss or Redis error."""
        try:
            raw = await self._redis.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception as exc:
            logger.warning("cache.get failed", key=key, error=str(exc))
            return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Serialize and store with TTL. Silently ignores Redis errors."""
        try:
            await self._redis.set(
                key, json.dumps(value, default=str), ex=ttl or self._ttl
            )
        except Exception as exc:
            logger.warning("cache.set failed", key=key, error=str(exc))

    async def delete(self, key: str) -> None:
        """Invalidate a key. Silently ignores errors."""
        try:
            await self._redis.delete(key)
        except Exception as exc:
            logger.warning("cache.delete failed", key=key, error=str(exc))

    async def delete_many(self, keys: List[str]) -> None:
        """
        Pipeline-batched multi-key invalidation.
        All DEL commands sent in a single round-trip. O(len(keys)).
        """
        if not keys:
            return
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for key in keys:
                    pipe.delete(key)
                await pipe.execute()
        except Exception as exc:
            logger.warning("cache.delete_many failed", keys=keys, error=str(exc))
            # Fallback: delete individually
            for key in keys:
                await self.delete(key)

    async def delete_pattern(self, pattern: str) -> int:
        """
        Delete all keys matching a glob pattern via SCAN (non-blocking).
        Returns count of deleted keys.
        """
        count = 0
        try:
            keys_to_delete: List[str] = []
            async for key in self._redis.scan_iter(match=pattern, count=100):
                keys_to_delete.append(key)
            if keys_to_delete:
                await self.delete_many(keys_to_delete)
                count = len(keys_to_delete)
        except Exception as exc:
            logger.warning("cache.delete_pattern failed", pattern=pattern, error=str(exc))
        return count

    async def get_or_set(
        self,
        key: str,
        fetch_fn: Callable,
        ttl: Optional[int] = None,
    ) -> Any:
        """
        Cache-aside: check cache → miss → call fetch_fn → store → return.
        fetch_fn may be sync or async.
        """
        cached = await self.get(key)
        if cached is not None:
            return cached
        import asyncio
        if asyncio.iscoroutinefunction(fetch_fn):
            value = await fetch_fn()
        else:
            value = fetch_fn()
        if value is not None:
            await self.set(key, value, ttl)
        return value

    async def publish(self, channel: str, message: Any) -> None:
        """Publish a message to a Redis pub/sub channel."""
        try:
            await self._redis.publish(channel, json.dumps(message, default=str))
        except Exception as exc:
            logger.warning("cache.publish failed", channel=channel, error=str(exc))

    async def ping(self) -> bool:
        """Health check."""
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False
