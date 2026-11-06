r"""
core/cache.py — Provider-agnostic Two-Tier Cache abstraction.

Architecture
------------
CacheBackend (ABC)          this file
  └─ SQLiteCache              core.sqlite_cache
  └─ RedisCache               core.redis_cache

Two-Tier Cache Hierarchy
------------------------
Tier 1: SHA256 Exact-Match (<1ms, $0 LLM cost)
  - Key: SHA256(org_id + ":" + normalized(clause_text) + ":" + clause_type + ":" + version)
  - Catches repeated standard/boilerplate clauses.

Tier 2: Semantic Cosine Match (with Guardrails)
  - Compares cosine similarity of clause embeddings (threshold >= 0.95).
  - Scoped to identical clause_type, policy version, and tenant-settings fingerprint.
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
import os
import re
from abc import ABC, abstractmethod
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
        tenant_settings: Optional[str] = None,
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
        tenant_settings: Optional[str] = None,
    ) -> None:
        """Store verdict under exact key and persist embedding for semantic tier."""

    @abstractmethod
    def invalidate(self, org_id: str) -> None:
        """Evict all cache entries for an org (called when policy content changes)."""

    @abstractmethod
    def get_policy_hash(self, org_id: str) -> Optional[str]:
        """Hash of the policy bytes last indexed for this company."""

    @abstractmethod
    def set_policy_hash(self, org_id: str, digest: str) -> None:
        """Remember the policy bytes now indexed. This does not store the PDF."""

    @abstractmethod
    def get_report(self, org_id: str, version: str, file_digest: str) -> Optional[dict]:
        """A finished contract report for these bytes and this policy version."""

    @abstractmethod
    def set_report(self, org_id: str, version: str, file_digest: str, report: dict) -> None:
        """Save a finished report. The file name is not part of the key."""

    @abstractmethod
    def drop_report(self, org_id: str, file_digest: str) -> None:
        """Forget saved reports for one file. Clause verdicts stay."""

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
        tenant_settings: str = "",
    ) -> str:
        """SHA256(org_id + ":" + normalize(clause_text) + ":" + clause_type + ":" + version + ":" + tenant_settings)."""
        norm = re.sub(r"\s+", " ", clause_text.strip().lower())
        c_type = (clause_type or "").strip().lower()
        ver = version or COMPLIANCE_EVALUATION_VERSION
        settings_str = (tenant_settings or "").strip().lower()
        payload = f"{org_id}:{norm}:{c_type}:{ver}:{settings_str}".encode("utf-8")
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


def pick_semantic_verdict(
    clause_text: str,
    embedding: list[float],
    rows: list[tuple[str, object, dict]],
    threshold: float,
) -> Optional[dict]:
    """Choose a near-duplicate verdict. The number and negation rules stay here.

    rows are (stored clause text, embedding, verdict). Redis and SQLite both
    call this. Neither database decides that "30 days" matches "90 days".
    """
    query = np.array(embedding, dtype=np.float32)
    query_numbers = CacheBackend.extract_numbers(clause_text)
    query_negations = CacheBackend.extract_negations(clause_text)
    candidates: list[tuple[float, str, dict]] = []
    for stored_text, stored_embedding, verdict in rows:
        vector = np.array(stored_embedding, dtype=np.float32)
        denom = float(np.linalg.norm(query) * np.linalg.norm(vector))
        similarity = 0.0 if denom == 0 else float(np.dot(query, vector) / denom)
        if similarity >= threshold:
            candidates.append((similarity, stored_text, verdict))
    candidates.sort(key=lambda item: item[0], reverse=True)
    for similarity, stored_text, verdict in candidates:
        if query_numbers != CacheBackend.extract_numbers(stored_text):
            continue
        stored_negations = CacheBackend.extract_negations(stored_text)
        if bool(query_negations) != bool(stored_negations) or query_negations != stored_negations:
            continue
        chosen = dict(verdict)
        chosen["cached"] = True
        chosen["cache_tier"] = "tier2_semantic"
        chosen["cache_similarity"] = round(similarity, 4)
        chosen["clause_text"] = clause_text
        return chosen
    return None


def resolve_cache_scope(
    org_id: str,
    version: Optional[str],
    tenant_settings: Optional[str],
) -> tuple[str, str]:
    """Fill the policy version and tenant fingerprint when the caller did not.

    Every cache implementation uses this so a new database does not invent
    its own fingerprint. A failed tenant lookup leaves the fingerprint empty
    and falls back to the evaluation version, which is the previous behavior.
    """
    if tenant_settings is None:
        try:
            from core.tenant import get_tenant_config
            cfg = get_tenant_config(org_id)
            tenant_settings = f"{cfg.silent_policy_behavior}:{cfg.favorable_deviation_behavior}"
            if version is None:
                version = cfg.active_policy_version
        except Exception:
            tenant_settings = ""
    ver = version or COMPLIANCE_EVALUATION_VERSION
    return ver, tenant_settings or ""


def file_hash(data: bytes) -> str:
    """Hash of one file's bytes. The file name is not included."""
    return hashlib.sha256(data).hexdigest()


def policy_set_hash(files: list[bytes]) -> str:
    """Hash of a set of policy files. Order and file names do not matter."""
    parts = sorted(file_hash(item) for item in files)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def get_cache() -> CacheBackend:
    """Return the configured cache backend (controlled by CACHE_BACKEND env var)."""
    backend = os.getenv("CACHE_BACKEND", "sqlite").lower()

    if backend == "sqlite":
        from core.sqlite_cache import SQLiteCache
        db_path = os.getenv("CACHE_DB_PATH") or os.getenv("SQLITE_DB_PATH")
        return SQLiteCache(db_path=db_path)

    if backend == "redis":
        from core.redis_cache import RedisCache
        return RedisCache(url=os.getenv("REDIS_URL"))

    raise ValueError(
        f"Unknown CACHE_BACKEND='{backend}'. Supported values: sqlite, redis"
    )

