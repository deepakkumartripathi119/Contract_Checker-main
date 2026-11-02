"""Redis implementation of the compliance-cache port.

Exact verdicts are keys. A near-duplicate is chosen by pick_semantic_verdict
in the application. A lost key only causes one new model call.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from core.cache import CacheBackend, pick_semantic_verdict, resolve_cache_scope


class RedisCache(CacheBackend):
    """Shared verdict cache. The PDF and the policy text are not stored here."""

    def __init__(self, url: Optional[str] = None, client=None):
        if client is not None:
            self.client = client
            return
        import redis

        self.client = redis.Redis.from_url(
            url or os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
        )

    @staticmethod
    def _verdict_key(key: str) -> str:
        return f"cc:v:{key}"

    @staticmethod
    def _org_key(org_id: str) -> str:
        return f"cc:org:{org_id}"

    def _read(self, key: str) -> Optional[dict]:
        raw = self.client.get(self._verdict_key(key))
        if not raw:
            return None
        return json.loads(raw)

    def _write(self, key: str, record: dict) -> None:
        self.client.set(self._verdict_key(key), json.dumps(record))
        self.client.sadd(self._org_key(record["org_id"]), key)

    def get(self, key: str) -> Optional[dict]:
        record = self._read(key)
        if record is None:
            return None
        return record["verdict"]

    def set(self, key: str, value: dict) -> None:
        self._write(
            key,
            {
                "org_id": value.get("org_id", "default"),
                "clause_text": value.get("clause_text", ""),
                "clause_type": value.get("clause_type", ""),
                "version": value.get("version", ""),
                "tenant_settings": value.get("tenant_settings", ""),
                "verdict": value,
                "embedding": None,
            },
        )

    def get_verdict(
        self,
        clause_text: str,
        org_id: str,
        clause_type: str = "",
        version: Optional[str] = None,
        embedding: Optional[list[float]] = None,
        threshold: float = 0.95,
        tenant_settings: Optional[str] = None,
    ) -> Optional[dict]:
        if not clause_text or not clause_text.strip():
            return None
        ver, settings_fp = resolve_cache_scope(org_id, version, tenant_settings)
        key = self.make_key(
            clause_text,
            org_id,
            clause_type=clause_type,
            version=ver,
            tenant_settings=settings_fp,
        )
        record = self._read(key)
        if record is not None:
            found = dict(record["verdict"])
            found["cached"] = True
            found["cache_tier"] = "tier1_exact"
            return found
        if embedding is None:
            return None

        names = self.client.smembers(self._org_key(org_id))
        if not names:
            return None
        raw_rows = self.client.mget([self._verdict_key(name) for name in names])
        rows = []
        for raw in raw_rows:
            if not raw:
                continue
            item = json.loads(raw)
            if item.get("version") != ver or item.get("tenant_settings") != settings_fp:
                continue
            if clause_type and item.get("clause_type") != clause_type:
                continue
            if not item.get("embedding"):
                continue
            rows.append((item["clause_text"], item["embedding"], item["verdict"]))
        return pick_semantic_verdict(clause_text, embedding, rows, threshold)

    def set_verdict(
        self,
        org_id: str,
        clause_text: str,
        verdict: dict,
        clause_type: str = "",
        version: Optional[str] = None,
        embedding: Optional[list[float]] = None,
        tenant_settings: Optional[str] = None,
    ) -> None:
        if not clause_text or not clause_text.strip():
            return
        ver, settings_fp = resolve_cache_scope(org_id, version, tenant_settings)
        key = self.make_key(
            clause_text,
            org_id,
            clause_type=clause_type,
            version=ver,
            tenant_settings=settings_fp,
        )
        self._write(
            key,
            {
                "org_id": org_id,
                "clause_text": clause_text,
                "clause_type": clause_type,
                "version": ver,
                "tenant_settings": settings_fp,
                "verdict": verdict,
                "embedding": [float(item) for item in embedding] if embedding else None,
            },
        )

    def invalidate(self, org_id: str) -> None:
        """Drop every saved verdict for this company. Called when its policy changes."""
        org_key = self._org_key(org_id)
        names = self.client.smembers(org_key)
        if names:
            self.client.delete(*[self._verdict_key(name) for name in names])
        self.client.delete(org_key)
