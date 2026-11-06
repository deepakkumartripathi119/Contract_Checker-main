"""
core/vector_store.py — Provider-agnostic vector store abstraction.

Architecture
------------
VectorStoreBackend (ABC)
  └─ SQLiteVectorStore   in core.sqlite_vector_store
  └─ PgVectorStore       in core.pg_vector_store

Swap backend by changing ONE env var — no other code changes needed.

Each stored chunk is org_id-scoped so multiple companies never share data.
The SQLite schema maps 1-to-1 to pgvector's layout (same column names),
making the migration a straight copy-paste of the table definition.

DB location: controlled by SQLITE_DB_PATH (default: data/contract_checker.db)

Phase 2 additions:
  - search_dense(org_id, query_embedding, top_k): returns (cosine_score, Chunk)
  - search_sparse(org_id, query_text, top_k): BM25Okapi sparse search returning (bm25_score, Chunk)
  - In-memory BM25 index per org_id with automatic cache invalidation
"""

from __future__ import annotations

import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional



# ---------------------------------------------------------------------------
# Chunk dataclass — the unit flowing through the whole pipeline
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    """
    A single piece of a document.

    Hierarchy
    ---------
    chunk_type="parent"  → full section; stored for LLM context
    chunk_type="child"   → sub-clause; embedded + searched
    parent_id=None       → this IS a parent (or a flat-doc leaf)
    parent_id=<uuid>     → pointer to its parent Chunk.id

    Future fields (add without breaking existing code):
      - language: str       (for multilingual policies)
      - effective_date: str (for policy versioning)
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    org_id: str = "default"
    doc_id: str = ""               # SHA256 of the source file bytes
    parent_id: Optional[str] = None
    chunk_type: str = "child"      # "parent" | "child"
    text: str = ""
    breadcrumb: str = ""           # "[Doc: X | Section 2 | Page 3]"
    metadata: dict = field(default_factory=dict)
    embedding: Optional[list] = None   # list[float]; None until embed step


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class VectorStoreBackend(ABC):
    """
    Common contract every vector store implementation must satisfy.

    Implementations must be stateless across calls so they can be swapped
    for a connection-pooled backend (pgvector, Qdrant, …) without changes
    to the pipeline nodes that call them.
    """

    @abstractmethod
    def add_chunks(self, org_id: str, chunks: list[Chunk]) -> None:
        """Persist a list of chunks (with embeddings already set)."""

    @abstractmethod
    def search_dense(
        self,
        org_id: str,
        query_embedding: list[float],
        top_k: int = 10,
    ) -> list[tuple[float, Chunk]]:
        """Return top_k (cosine_similarity, Chunk) for org_id."""

    @abstractmethod
    def search_sparse(
        self,
        org_id: str,
        query_text: str,
        top_k: int = 10,
    ) -> list[tuple[float, Chunk]]:
        """Return top_k (sparse_score, Chunk) for org_id using BM25."""

    @abstractmethod
    def search(
        self,
        org_id: str,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[Chunk]:
        """Backward-compatible dense search returning just chunks."""

    @abstractmethod
    def get_parent(self, parent_id: str, org_id: Optional[str] = None) -> Optional[Chunk]:
        """Fetch a parent chunk by its id, scoped to org_id when provided."""

    @abstractmethod
    def clear_org(self, org_id: str) -> None:
        """Delete all chunks for an org (used when re-uploading a policy)."""

    @abstractmethod
    def export_org_chunks(self, org_id: str) -> list[Chunk]:
        """Return every chunk for an org, including embeddings, for rollback."""

    @abstractmethod
    def replace_org_chunks(self, org_id: str, chunks: list[Chunk], alongside=None) -> None:
        """Atomically replace all chunks for an org within a single transaction."""

    @abstractmethod
    def list_docs(self, org_id: str) -> list[str]:
        """Return distinct doc_ids ingested for an org."""

    @abstractmethod
    def get_policy_sections(self, org_id: str) -> list[Chunk]:
        """Fetch all top-level policy section chunks for an org."""

# ---------------------------------------------------------------------------
# Factory — the ONLY place that knows which backend is active
# ---------------------------------------------------------------------------

def get_vector_store() -> VectorStoreBackend:
    """
    Return the configured vector store backend.

    Controlled by VECTOR_STORE_BACKEND env var:
      sqlite   → SQLiteVectorStore  (default)
      pgvector → PgVectorStore      (DATABASE_URL required)

    Callers use get_vector_store(). The SQLite and Postgres classes live in
    their own modules and are imported only inside this function.
    """
    backend = os.getenv("VECTOR_STORE_BACKEND", "sqlite").lower()

    if backend == "sqlite":
        from core.sqlite_vector_store import SQLiteVectorStore
        db_path = os.getenv("SQLITE_DB_PATH")
        return SQLiteVectorStore(db_path=db_path)

    if backend == "pgvector":
        from core.pg_vector_store import PgVectorStore
        return PgVectorStore()

    raise ValueError(
        f"Unknown VECTOR_STORE_BACKEND='{backend}'. "
        "Supported values: sqlite, pgvector"
    )
