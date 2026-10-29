r"""
core/cache.py — Provider-agnostic Two-Tier Cache abstraction.

Architecture
------------
CacheBackend (ABC)
  └─ SQLiteCache   ← current default (CACHE_BACKEND=sqlite)
  └─ RedisCache    ← future  (CACHE_BACKEND=redis)

Two-Tier Cache Hierarchy
------------------------
Tier 1: SHA256 Exact-Match (<1ms, $0 LLM cost)
  - Key: SHA256(org_id + ":" + normalized(clause_text) + ":" + clause_type + ":" + version)
  - Catches repeated standard/boilerplate clauses.

Tier 2: Semantic Cosine Match (with Guardrails)
  - Compares cosine similarity of clause embeddings (threshold >= 0.95).
  - Scoped to identical clause_type and active compliance rule version.
  - CRITICAL GUARDS to prevent false semantic equivalence:
    1. Numeric Guard: Extracts all numbers (\b\d+(?:\.\d+)?\b). If numbers differ
       (e.g., "30 days" vs "90 days"), the match is REJECTED.
    2. Negation Guard: Detects negation polarities (e.g., "shall indemnify" vs
       "shall not indemnify"). If polarities differ, the match is REJECTED.

Invalidation
------------
Calling invalidate(org_id) evicts all cached entries when an organisation
updates their policy documents.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import numpy as np
from langsmith import traceable


COMPLIANCE_EVALUATION_VERSION = os.getenv("COMPLIANCE_EVALUATION_VERSION", "v1.0")

_NEGATION_TOKENS = {
    "not", "no", "never", "neither", "nor", "none", "without",
    "except", "excluding", "cannot", "prohibited", "disallowed",
    "unauthorized", "refuse", "deny",
}


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class CacheBackend(ABC):
    """
    Key-value and semantic cache for compliance verdicts.
    """

    @abstractmethod
    def get(self, key: str) -> Optional[dict]:
        """Direct exact lookup by key."""

    @abstractmethod
    def set(self, key: str, value: dict) -> None:
        """Direct exact store by key."""

    @abstractmethod
    @traceable(name="cache_get")
    def get_verdict(
        self,
        clause_text: str,
        org_id: str,
        clause_type: str = "",
        version: Optional[str] = None,
        embedding: Optional[list[float]] = None,
        threshold: float = 0.95,
    ) -> Optional[dict]:
        """
        Two-tier lookup: Tier-1 exact match, then Tier-2 semantic match with guards.
        """

    @abstractmethod
    @traceable(name="cache_set")
    def set_verdict(
        self,
        org_id: str,
        clause_text: str,
        verdict: dict,
        clause_type: str = "",
        version: Optional[str] = None,
        embedding: Optional[list[float]] = None,
    ) -> None:
        """Store verdict under exact key and persist embedding for semantic tier."""

    @abstractmethod
    def invalidate(self, org_id: str) -> None:
        """Evict all cache entries for an org (called when policy is updated)."""

    @staticmethod
    def normalize_text(text: str) -> str:
        """Normalize text: strip, lowercase, collapse whitespace."""
        return re.sub(r"\s+", " ", text.strip().lower())

    @staticmethod
    def make_key(
        clause_text: str,
        org_id: str,
        clause_type: str = "",
        version: str = "",
    ) -> str:
        """SHA256(org_id + ":" + normalize(clause_text) + ":" + clause_type + ":" + version)."""
        norm = re.sub(r"\s+", " ", clause_text.strip().lower())
        c_type = (clause_type or "").strip().lower()
        ver = version or COMPLIANCE_EVALUATION_VERSION
        payload = f"{org_id}:{norm}:{c_type}:{ver}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def extract_numbers(text: str) -> list[str]:
        """Extract all numeric tokens for the numeric guard."""
        return re.findall(r"\b\d+(?:\.\d+)?\b", text)

    @staticmethod
    def extract_negations(text: str) -> set[str]:
        """Extract all negation tokens for the negation polarity guard."""
        tokens = set(re.findall(r"\w+", text.lower()))
        return tokens.intersection(_NEGATION_TOKENS)


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------

_CREATE_CACHE_TABLE = """
CREATE TABLE IF NOT EXISTS compliance_cache (
    cache_key     TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL,
    clause_text   TEXT NOT NULL,
    clause_type   TEXT NOT NULL DEFAULT '',
    version       TEXT NOT NULL DEFAULT 'v1.0',
    value         TEXT NOT NULL,
    embedding     BLOB,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cache_org ON compliance_cache(org_id);
"""


class SQLiteCache(CacheBackend):
    """
    SQLite-backed two-tier cache with exact SHA256 and semantic cosine matching.
    """

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
            # Automatic schema migration for existing databases
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_org_type_ver ON compliance_cache(org_id, clause_type, version);")

    @staticmethod
    def _embed_to_blob(embedding: list[float]) -> bytes:
        return np.array(embedding, dtype=np.float32).tobytes()

    @staticmethod
    def _blob_to_embed(blob: bytes) -> np.ndarray:
        return np.frombuffer(blob, dtype=np.float32)

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        if denom == 0:
            return 0.0
        return float(np.dot(a, b) / denom)

    # ------------------------------------------------------------------
    # CacheBackend interface
    # ------------------------------------------------------------------

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
    ) -> Optional[dict]:
        """
        Two-tier lookup:
        1. Tier 1: SHA256 exact match on normalized clause text, org_id, clause_type, and version.
        2. Tier 2: Semantic cosine match with numeric-value and negation guards.
        """
        if not clause_text or not clause_text.strip():
            return None

        ver = version or COMPLIANCE_EVALUATION_VERSION
        key = self.make_key(clause_text, org_id, clause_type=clause_type, version=ver)

        # --- Tier 1: Exact Match (<1ms) ---
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

        # --- Tier 2: Semantic Match with Guardrails ---
        if embedding is None:
            return None

        qvec = np.array(embedding, dtype=np.float32)
        q_nums = self.extract_numbers(clause_text)
        q_negs = self.extract_negations(clause_text)

        params: list[str] = [org_id, ver]
        clause_type_filter = ""
        if clause_type:
            clause_type_filter = "AND clause_type = ?"
            params.append(clause_type)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT cache_key, clause_text, value, embedding
                FROM   compliance_cache
                WHERE  org_id = ? AND version = ? {clause_type_filter} AND embedding IS NOT NULL
                """,
                params,
            ).fetchall()

        if not rows:
            return None

        candidates: list[tuple[float, str, dict]] = []
        for r in rows:
            vec = self._blob_to_embed(r["embedding"])
            sim = self._cosine(qvec, vec)
            if sim >= threshold:
                candidates.append((sim, r["clause_text"], json.loads(r["value"])))

        if not candidates:
            return None

        # Sort candidates descending by similarity
        candidates.sort(key=lambda x: x[0], reverse=True)

        for sim, cand_text, verdict in candidates:
            # 1. Numeric Guard: If numbers differ (e.g. 30 vs 90 days), reject match
            c_nums = self.extract_numbers(cand_text)
            if q_nums != c_nums:
                continue

            # 2. Negation Guard: If negation polarity differs, reject match
            c_negs = self.extract_negations(cand_text)
            if bool(q_negs) != bool(c_negs) or q_negs != c_negs:
                continue

            # Match accepted!
            verdict["cached"] = True
            verdict["cache_tier"] = "tier2_semantic"
            verdict["cache_similarity"] = round(sim, 4)
            # Update clause_text to the query clause for report consistency
            verdict["clause_text"] = clause_text
            return verdict

        return None

    def set_verdict(
        self,
        org_id: str,
        clause_text: str,
        verdict: dict,
        clause_type: str = "",
        version: Optional[str] = None,
        embedding: Optional[list[float]] = None,
    ) -> None:
        """Store verdict with both exact key and optional embedding for semantic tier."""
        if not clause_text or not clause_text.strip():
            return

        ver = version or COMPLIANCE_EVALUATION_VERSION
        key = self.make_key(clause_text, org_id, clause_type=clause_type, version=ver)
        blob = self._embed_to_blob(embedding) if embedding else None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO compliance_cache
                  (cache_key, org_id, clause_text, clause_type, version, value, embedding)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    org_id,
                    clause_text,
                    clause_type,
                    ver,
                    json.dumps(verdict),
                    blob,
                ),
            )

    def invalidate(self, org_id: str) -> None:
        """Evict all cached entries for an org (called on policy document re-upload)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM compliance_cache WHERE org_id = ?", (org_id,))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_cache() -> CacheBackend:
    """Return the configured cache backend (controlled by CACHE_BACKEND env var)."""
    backend = os.getenv("CACHE_BACKEND", "sqlite").lower()

    if backend == "sqlite":
        db_path = os.getenv("SQLITE_DB_PATH")
        return SQLiteCache(db_path=db_path)

    if backend == "redis":
        raise NotImplementedError(
            "Redis cache backend is not yet implemented. Set CACHE_BACKEND=sqlite."
        )

    raise ValueError(
        f"Unknown CACHE_BACKEND='{backend}'. Supported values: sqlite, redis"
    )
