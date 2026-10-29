"""
core/vector_store.py — Provider-agnostic vector store abstraction.

Architecture
------------
VectorStoreBackend (ABC)
  └─ SQLiteVectorStore   ← current default (VECTOR_STORE_BACKEND=sqlite)
  └─ PgVectorStore       ← future  (VECTOR_STORE_BACKEND=pgvector)

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

import json
import os
import re
import sqlite3
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from rank_bm25 import BM25Okapi


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
    def get_parent(self, parent_id: str) -> Optional[Chunk]:
        """Fetch a parent chunk by its id (cross-org — id is a global UUID)."""

    @abstractmethod
    def clear_org(self, org_id: str) -> None:
        """Delete all chunks for an org (used when re-uploading a policy)."""

    @abstractmethod
    def list_docs(self, org_id: str) -> list[str]:
        """Return distinct doc_ids ingested for an org."""

    @abstractmethod
    def get_policy_sections(self, org_id: str) -> list[Chunk]:
        """Fetch all top-level policy section chunks for an org."""


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS chunks (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    parent_id   TEXT,
    chunk_type  TEXT NOT NULL DEFAULT 'child',
    text        TEXT NOT NULL,
    breadcrumb  TEXT NOT NULL DEFAULT '',
    metadata    TEXT NOT NULL DEFAULT '{}',
    embedding   BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_org    ON chunks(org_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc    ON chunks(org_id, doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id);
"""


class SQLiteVectorStore(VectorStoreBackend):
    """
    SQLite-backed vector store using:
      1. Cosine similarity via numpy for dense search
      2. BM25Okapi for sparse keyword search
      3. Global UUID-based parent chunk lookup

    Thread safety: each call opens its own connection (check_same_thread=False
    is set for compatibility with Streamlit's threading model).
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.getenv(
            "SQLITE_DB_PATH",
            str(Path(__file__).resolve().parents[1] / "data" / "contract_checker.db"),
        )
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._bm25_indices: dict[str, tuple[BM25Okapi, list[Chunk]]] = {}
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
            conn.executescript(_CREATE_TABLE)

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

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Simple whitespace and word tokenization for BM25."""
        return re.findall(r"\w+", text.lower())

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row) -> Chunk:
        return Chunk(
            id=row["id"],
            org_id=row["org_id"],
            doc_id=row["doc_id"],
            parent_id=row["parent_id"],
            chunk_type=row["chunk_type"],
            text=row["text"],
            breadcrumb=row["breadcrumb"],
            metadata=json.loads(row["metadata"]),
            embedding=None,  # not returned for perf; re-fetch if needed
        )

    def _get_bm25(self, org_id: str) -> tuple[Optional[BM25Okapi], list[Chunk]]:
        """Return or build the in-memory BM25 index for the given org."""
        if org_id in self._bm25_indices:
            return self._bm25_indices[org_id]

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, org_id, doc_id, parent_id, chunk_type,
                       text, breadcrumb, metadata
                FROM   chunks
                WHERE  org_id = ? AND chunk_type = 'child'
                """,
                (org_id,),
            ).fetchall()

        if not rows:
            return None, []

        chunks = [self._row_to_chunk(r) for r in rows]
        tokenized_corpus = [self._tokenize(f"{c.breadcrumb} {c.text}") for c in chunks]
        bm25 = BM25Okapi(tokenized_corpus)

        # Guard against zero/negative IDF on small corpora (e.g. <= 2 docs)
        for word in bm25.idf:
            if bm25.idf[word] <= 0:
                bm25.idf[word] = 0.5

        self._bm25_indices[org_id] = (bm25, chunks)
        return bm25, chunks

    # ------------------------------------------------------------------
    # VectorStoreBackend interface
    # ------------------------------------------------------------------

    def add_chunks(self, org_id: str, chunks: list[Chunk]) -> None:
        """Insert or replace chunks. Embeddings must be set on child chunks."""
        rows = []
        for c in chunks:
            blob = self._embed_to_blob(c.embedding) if c.embedding else None
            rows.append((
                c.id,
                org_id,
                c.doc_id,
                c.parent_id,
                c.chunk_type,
                c.text,
                c.breadcrumb,
                json.dumps(c.metadata),
                blob,
            ))
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO chunks
                  (id, org_id, doc_id, parent_id, chunk_type,
                   text, breadcrumb, metadata, embedding)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
        # Invalidate in-memory BM25 index for this org
        self._bm25_indices.pop(org_id, None)

    def search_dense(
        self,
        org_id: str,
        query_embedding: list[float],
        top_k: int = 10,
    ) -> list[tuple[float, Chunk]]:
        """
        Cosine similarity search over child chunks for org_id.
        Returns list of (cosine_score, Chunk) sorted descending by score.
        """
        qvec = np.array(query_embedding, dtype=np.float32)

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, org_id, doc_id, parent_id, chunk_type,
                       text, breadcrumb, metadata, embedding
                FROM   chunks
                WHERE  org_id = ? AND chunk_type = 'child' AND embedding IS NOT NULL
                """,
                (org_id,),
            ).fetchall()

        if not rows:
            return []

        scored: list[tuple[float, Chunk]] = []
        for row in rows:
            vec = self._blob_to_embed(row["embedding"])
            score = self._cosine(qvec, vec)
            chunk = self._row_to_chunk(row)
            scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

    def search_sparse(
        self,
        org_id: str,
        query_text: str,
        top_k: int = 10,
    ) -> list[tuple[float, Chunk]]:
        """
        BM25Okapi keyword search over child chunks for org_id.
        Returns list of (bm25_score, Chunk) sorted descending by score.
        """
        bm25, chunks = self._get_bm25(org_id)
        if not bm25 or not chunks:
            return []

        tokens = self._tokenize(query_text)
        if not tokens:
            return []

        scores = bm25.get_scores(tokens)
        scored: list[tuple[float, Chunk]] = [
            (float(scores[i]), chunks[i])
            for i in range(len(chunks))
            if scores[i] > 0
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

    def search(
        self,
        org_id: str,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[Chunk]:
        """
        Backward-compatible dense search returning just chunks.
        """
        hits = self.search_dense(org_id, query_embedding, top_k=top_k)
        return [c for _, c in hits]

    def get_parent(self, parent_id: str) -> Optional[Chunk]:
        """Fetch a parent chunk by id (org-agnostic — id is globally unique)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chunks WHERE id = ?", (parent_id,)
            ).fetchone()
        return self._row_to_chunk(row) if row else None

    def clear_org(self, org_id: str) -> None:
        """Remove all chunks for an org — call before re-ingesting a policy."""
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE org_id = ?", (org_id,))
        self._bm25_indices.pop(org_id, None)

    def list_docs(self, org_id: str) -> list[str]:
        """Return distinct doc_ids for an org."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT doc_id FROM chunks WHERE org_id = ?", (org_id,)
            ).fetchall()
        return [r["doc_id"] for r in rows]

    def get_policy_sections(self, org_id: str) -> list[Chunk]:
        """Return parent policy chunks for org_id (or child chunks if flat policy)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, org_id, doc_id, parent_id, chunk_type,
                       text, breadcrumb, metadata
                FROM   chunks
                WHERE  org_id = ? AND chunk_type = 'parent'
                """,
                (org_id,),
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    """
                    SELECT id, org_id, doc_id, parent_id, chunk_type,
                           text, breadcrumb, metadata
                    FROM   chunks
                    WHERE  org_id = ?
                    """,
                    (org_id,),
                ).fetchall()
        return [self._row_to_chunk(r) for r in rows]


# ---------------------------------------------------------------------------
# Factory — the ONLY place that knows which backend is active
# ---------------------------------------------------------------------------

def get_vector_store() -> VectorStoreBackend:
    """
    Return the configured vector store backend.

    Controlled by VECTOR_STORE_BACKEND env var:
      sqlite   → SQLiteVectorStore  (default)
      pgvector → PgVectorStore      (TODO: implement when moving to Postgres)

    Callers never import SQLiteVectorStore directly — they call get_vector_store().
    Switching backends requires zero changes outside this function.
    """
    backend = os.getenv("VECTOR_STORE_BACKEND", "sqlite").lower()

    if backend == "sqlite":
        db_path = os.getenv("SQLITE_DB_PATH")
        return SQLiteVectorStore(db_path=db_path)

    # Stub — fill in when PgVectorStore is implemented
    if backend == "pgvector":
        raise NotImplementedError(
            "pgvector backend is not yet implemented. "
            "Set VECTOR_STORE_BACKEND=sqlite for now."
        )

    raise ValueError(
        f"Unknown VECTOR_STORE_BACKEND='{backend}'. "
        "Supported values: sqlite, pgvector"
    )
