"""SQLite implementation of the vector-store port.

The port itself lives in core.vector_store. This file is the only place that
knows the SQLite table.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

from core.vector_store import Chunk, VectorStoreBackend

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
        from core.bm25_index import build_index

        bm25 = build_index(chunks)
        if bm25 is None:
            return None, []
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
        from core.bm25_index import rank

        return rank(bm25, chunks, query_text, top_k)

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

    def get_parent(self, parent_id: str, org_id: Optional[str] = None) -> Optional[Chunk]:
        """Fetch a parent chunk by id scoped to org_id when provided."""
        with self._connect() as conn:
            if org_id:
                row = conn.execute(
                    "SELECT * FROM chunks WHERE id = ? AND org_id = ?",
                    (parent_id, org_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM chunks WHERE id = ?", (parent_id,)
                ).fetchone()
        return self._row_to_chunk(row) if row else None

    def clear_org(self, org_id: str) -> None:
        """Remove all chunks for an org — call before re-ingesting a policy."""
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE org_id = ?", (org_id,))
        self._bm25_indices.pop(org_id, None)

    def export_org_chunks(self, org_id: str) -> list[Chunk]:
        """Return every chunk for an org, including stored embeddings."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE org_id = ?",
                (org_id,),
            ).fetchall()
        exported: list[Chunk] = []
        for row in rows:
            chunk = self._row_to_chunk(row)
            if row["embedding"] is not None:
                chunk.embedding = self._blob_to_embed(row["embedding"]).tolist()
            exported.append(chunk)
        return exported

    def replace_org_chunks(self, org_id: str, chunks: list[Chunk]) -> None:
        """Atomically replace all chunks for an org within a single transaction."""
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
            conn.execute("DELETE FROM chunks WHERE org_id = ?", (org_id,))
            conn.executemany(
                """
                INSERT OR REPLACE INTO chunks
                  (id, org_id, doc_id, parent_id, chunk_type,
                   text, breadcrumb, metadata, embedding)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
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


