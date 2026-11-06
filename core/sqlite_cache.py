"""SQLite implementation of the compliance-cache port.

The port, cache key, and tenant fingerprint live in core.cache. This file
only knows the SQLite table. A Redis implementation would be a sibling file
and would call the same key and fingerprint helpers.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

from core.cache import (
    COMPLIANCE_EVALUATION_VERSION,
    CacheBackend,
    pick_semantic_verdict,
    resolve_cache_scope,
)


_CREATE_CACHE_TABLE = """
CREATE TABLE IF NOT EXISTS compliance_cache (
    cache_key     TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL,
    clause_text   TEXT NOT NULL,
    clause_type   TEXT NOT NULL DEFAULT '',
    version       TEXT NOT NULL DEFAULT 'v1.0',
    tenant_settings TEXT NOT NULL DEFAULT '',
    value         TEXT NOT NULL,
    embedding     BLOB,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cache_org ON compliance_cache(org_id);
CREATE TABLE IF NOT EXISTS content_cache (
    cache_key TEXT PRIMARY KEY,
    org_id    TEXT NOT NULL,
    value     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_org ON content_cache(org_id);
"""


class SQLiteCache(CacheBackend):
    """SQLite-backed two-tier cache with exact SHA256 and semantic cosine matching."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.getenv(
            "SQLITE_DB_PATH",
            str(Path(__file__).resolve().parents[1] / "data" / "contract_checker.db"),
        )
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            if not hasattr(self, "_mem_conn") or self._mem_conn is None:
                self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._mem_conn.row_factory = sqlite3.Row
            return self._mem_conn
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_CREATE_CACHE_TABLE)
            cursor = conn.execute("PRAGMA table_info(compliance_cache)")
            columns = {row["name"] for row in cursor.fetchall()}
            if "clause_text" not in columns:
                conn.execute("ALTER TABLE compliance_cache ADD COLUMN clause_text TEXT NOT NULL DEFAULT ''")
            if "embedding" not in columns:
                conn.execute("ALTER TABLE compliance_cache ADD COLUMN embedding BLOB")
            if "clause_type" not in columns:
                conn.execute("ALTER TABLE compliance_cache ADD COLUMN clause_type TEXT NOT NULL DEFAULT ''")
            if "version" not in columns:
                conn.execute("ALTER TABLE compliance_cache ADD COLUMN version TEXT NOT NULL DEFAULT 'v1.0'")
            if "tenant_settings" not in columns:
                conn.execute(
                    "ALTER TABLE compliance_cache ADD COLUMN tenant_settings TEXT NOT NULL DEFAULT ''"
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_org_type_ver ON compliance_cache(org_id, clause_type, version);")

    @staticmethod
    def _embed_to_blob(embedding: list[float]) -> bytes:
        return np.array(embedding, dtype=np.float32).tobytes()

    @staticmethod
    def _blob_to_embed(blob: bytes) -> np.ndarray:
        return np.frombuffer(blob, dtype=np.float32)

    def get(self, key: str) -> Optional[dict]:
        """Direct exact lookup by key."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM compliance_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        return json.loads(row["value"])

    def set(self, key: str, value: dict) -> None:
        """Direct store under exact key."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO compliance_cache
                  (cache_key, org_id, clause_text, clause_type, version, value, embedding)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    value.get("org_id", "default"),
                    value.get("clause_text", ""),
                    value.get("clause_type", ""),
                    value.get("version", COMPLIANCE_EVALUATION_VERSION),
                    json.dumps(value),
                    None,
                ),
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
        """Two-tier lookup: exact key, then semantic match with numeric and negation guards."""
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

        with self._connect() as conn:
            row = conn.execute(
                "SELECT value, clause_text FROM compliance_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()

        if row:
            res = json.loads(row["value"])
            res["cached"] = True
            res["cache_tier"] = "tier1_exact"
            return res

        if embedding is None:
            return None

        params: list[str] = [org_id, ver, settings_fp]
        clause_type_filter = ""
        if clause_type:
            clause_type_filter = "AND clause_type = ?"
            params.append(clause_type)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT cache_key, clause_text, value, embedding
                FROM   compliance_cache
                WHERE  org_id = ? AND version = ? AND tenant_settings = ?
                       {clause_type_filter} AND embedding IS NOT NULL
                """,
                params,
            ).fetchall()

        if not rows:
            return None
        return pick_semantic_verdict(
            clause_text,
            embedding,
            [
                (row["clause_text"], self._blob_to_embed(row["embedding"]), json.loads(row["value"]))
                for row in rows
            ],
            threshold,
        )

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
        """Store a verdict under the exact key and keep the embedding for the semantic tier."""
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
        blob = self._embed_to_blob(embedding) if embedding else None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO compliance_cache
                  (cache_key, org_id, clause_text, clause_type, version, tenant_settings, value, embedding)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    org_id,
                    clause_text,
                    clause_type,
                    ver,
                    settings_fp,
                    json.dumps(verdict),
                    blob,
                ),
            )

    def _content_key(self, kind: str, org_id: str, *parts: str) -> str:
        return ":".join((kind, org_id, *parts))

    def get_policy_hash(self, org_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM content_cache WHERE cache_key = ?",
                (self._content_key("policy", org_id),),
            ).fetchone()
        return row["value"] if row else None

    def set_policy_hash(self, org_id: str, digest: str) -> None:
        key = self._content_key("policy", org_id)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO content_cache (cache_key, org_id, value)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET value = excluded.value
                """,
                (key, org_id, digest),
            )

    def get_report(self, org_id: str, version: str, file_digest: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM content_cache WHERE cache_key = ?",
                (self._content_key("report", org_id, version, file_digest),),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["value"])

    def set_report(self, org_id: str, version: str, file_digest: str, report: dict) -> None:
        key = self._content_key("report", org_id, version, file_digest)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO content_cache (cache_key, org_id, value)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET value = excluded.value
                """,
                (key, org_id, json.dumps(report)),
            )

    def drop_report(self, org_id: str, file_digest: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM content_cache WHERE org_id = ? AND cache_key LIKE ?",
                (org_id, f"report:{org_id}:%:{file_digest}"),
            )

    def invalidate(self, org_id: str) -> None:
        """Evict verdicts and reports. The policy hash is replaced by the caller."""
        with self._connect() as conn:
            conn.execute("DELETE FROM compliance_cache WHERE org_id = ?", (org_id,))
            conn.execute(
                "DELETE FROM content_cache WHERE org_id = ? AND cache_key LIKE ?",
                (org_id, f"report:{org_id}:%"),
            )
