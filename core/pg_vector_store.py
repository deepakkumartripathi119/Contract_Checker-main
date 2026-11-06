"""Postgres + pgvector implementation of the vector store.

Dense search uses pgvector cosine distance. Sparse search stays BM25 in memory,
with the same tokenization and IDF guard as the SQLite store. RRF weights are
not set here.
"""

from __future__ import annotations

import json
import os
from typing import Optional

import numpy as np
from rank_bm25 import BM25Okapi
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from core.pg_schema import EMBEDDING_DIMENSIONS, apply_schema
from core.vector_store import Chunk, VectorStoreBackend


def _vector_literal(embedding: list[float]) -> str:
    if len(embedding) != EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"Embedding length {len(embedding)} does not match {EMBEDDING_DIMENSIONS}."
        )
    return "[" + ",".join(str(float(value)) for value in embedding) + "]"


def _row_to_chunk(row, embedding: Optional[list[float]] = None) -> Chunk:
    metadata = row.metadata
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return Chunk(
        id=row.id,
        org_id=row.org_id,
        doc_id=row.doc_id,
        parent_id=row.parent_id,
        chunk_type=row.chunk_type,
        text=row.text,
        breadcrumb=row.breadcrumb,
        metadata=metadata or {},
        embedding=embedding,
    )


class PgVectorStore(VectorStoreBackend):
    def __init__(self, database_url: Optional[str] = None):
        self.database_url = database_url or os.getenv("DATABASE_URL")
        if not self.database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. PgVectorStore cannot connect."
            )
        self.engine: Engine = create_engine(self.database_url)
        self._bm25_indices: dict[str, tuple[BM25Okapi, list[Chunk]]] = {}
        with self.engine.begin() as connection:
            apply_schema(connection)

    def add_chunks(self, org_id: str, chunks: list[Chunk]) -> None:
        ordered = sorted(chunks, key=lambda chunk: chunk.parent_id is not None)
        with self.engine.begin() as connection:
            for chunk in ordered:
                embedding = (
                    _vector_literal(chunk.embedding) if chunk.embedding else None
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO policy_chunks (
                            id, org_id, doc_id, parent_id, chunk_type,
                            text, breadcrumb, metadata, embedding
                        ) VALUES (
                            :id, :org_id, :doc_id, :parent_id, :chunk_type,
                            :text, :breadcrumb, CAST(:metadata AS jsonb),
                            CAST(:embedding AS vector)
                        )
                        ON CONFLICT (id) DO UPDATE SET
                            org_id = EXCLUDED.org_id,
                            doc_id = EXCLUDED.doc_id,
                            parent_id = EXCLUDED.parent_id,
                            chunk_type = EXCLUDED.chunk_type,
                            text = EXCLUDED.text,
                            breadcrumb = EXCLUDED.breadcrumb,
                            metadata = EXCLUDED.metadata,
                            embedding = EXCLUDED.embedding
                        """
                    ),
                    {
                        "id": chunk.id,
                        "org_id": org_id,
                        "doc_id": chunk.doc_id,
                        "parent_id": chunk.parent_id,
                        "chunk_type": chunk.chunk_type,
                        "text": chunk.text,
                        "breadcrumb": chunk.breadcrumb,
                        "metadata": json.dumps(chunk.metadata),
                        "embedding": embedding,
                    },
                )
        self._bm25_indices.pop(org_id, None)

    def search_dense(
        self,
        org_id: str,
        query_embedding: list[float],
        top_k: int = 10,
    ) -> list[tuple[float, Chunk]]:
        query = _vector_literal(query_embedding)
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT id, org_id, doc_id, parent_id, chunk_type,
                           text, breadcrumb, metadata,
                           1 - (embedding <=> CAST(:query AS vector)) AS score
                    FROM policy_chunks
                    WHERE org_id = :org_id
                      AND chunk_type = 'child'
                      AND embedding IS NOT NULL
                    ORDER BY embedding <=> CAST(:query AS vector)
                    LIMIT :top_k
                    """
                ),
                {"org_id": org_id, "query": query, "top_k": top_k},
            ).fetchall()
        return [(float(row.score), _row_to_chunk(row)) for row in rows]

    def search_sparse(
        self,
        org_id: str,
        query_text: str,
        top_k: int = 10,
    ) -> list[tuple[float, Chunk]]:
        bm25, chunks = self._get_bm25(org_id)
        from core.bm25_index import rank

        return rank(bm25, chunks, query_text, top_k)

    def search(
        self,
        org_id: str,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[Chunk]:
        return [chunk for _, chunk in self.search_dense(org_id, query_embedding, top_k)]

    def get_parent(self, parent_id: str, org_id: Optional[str] = None) -> Optional[Chunk]:
        with self.engine.connect() as connection:
            if org_id:
                row = connection.execute(
                    text(
                        """
                        SELECT id, org_id, doc_id, parent_id, chunk_type,
                               text, breadcrumb, metadata
                        FROM policy_chunks
                        WHERE id = :id AND org_id = :org_id
                        """
                    ),
                    {"id": parent_id, "org_id": org_id},
                ).fetchone()
            else:
                row = connection.execute(
                    text(
                        """
                        SELECT id, org_id, doc_id, parent_id, chunk_type,
                               text, breadcrumb, metadata
                        FROM policy_chunks WHERE id = :id
                        """
                    ),
                    {"id": parent_id},
                ).fetchone()
        return _row_to_chunk(row) if row else None

    def clear_org(self, org_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    DELETE FROM policy_chunks
                    WHERE org_id = :org_id AND chunk_type = 'child'
                    """
                ),
                {"org_id": org_id},
            )
            connection.execute(
                text("DELETE FROM policy_chunks WHERE org_id = :org_id"),
                {"org_id": org_id},
            )
        self._bm25_indices.pop(org_id, None)

    def export_org_chunks(self, org_id: str) -> list[Chunk]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT id, org_id, doc_id, parent_id, chunk_type,
                           text, breadcrumb, metadata, embedding::text AS embedding_text
                    FROM policy_chunks
                    WHERE org_id = :org_id
                    """
                ),
                {"org_id": org_id},
            ).fetchall()
        exported = []
        for row in rows:
            embedding = None
            if row.embedding_text:
                embedding = [float(value) for value in row.embedding_text.strip("[]").split(",") if value]
            exported.append(_row_to_chunk(row, embedding=embedding))
        return exported

    def replace_org_chunks(self, org_id: str, chunks: list[Chunk], alongside=None) -> None:
        ordered = sorted(chunks, key=lambda chunk: chunk.parent_id is not None)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    DELETE FROM policy_chunks
                    WHERE org_id = :org_id AND chunk_type = 'child'
                    """
                ),
                {"org_id": org_id},
            )
            connection.execute(
                text("DELETE FROM policy_chunks WHERE org_id = :org_id"),
                {"org_id": org_id},
            )
            for chunk in ordered:
                embedding = (
                    _vector_literal(chunk.embedding) if chunk.embedding else None
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO policy_chunks (
                            id, org_id, doc_id, parent_id, chunk_type,
                            text, breadcrumb, metadata, embedding
                        ) VALUES (
                            :id, :org_id, :doc_id, :parent_id, :chunk_type,
                            :text, :breadcrumb, CAST(:metadata AS jsonb),
                            CAST(:embedding AS vector)
                        )
                        """
                    ),
                    {
                        "id": chunk.id,
                        "org_id": org_id,
                        "doc_id": chunk.doc_id,
                        "parent_id": chunk.parent_id,
                        "chunk_type": chunk.chunk_type,
                        "text": chunk.text,
                        "breadcrumb": chunk.breadcrumb,
                        "metadata": json.dumps(chunk.metadata),
                        "embedding": embedding,
                    },
                )
            if alongside is not None:
                alongside(connection)
        self._bm25_indices.pop(org_id, None)

    def list_docs(self, org_id: str) -> list[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT DISTINCT doc_id FROM policy_chunks WHERE org_id = :org_id"
                ),
                {"org_id": org_id},
            ).fetchall()
        return [row.doc_id for row in rows]

    def get_policy_sections(self, org_id: str) -> list[Chunk]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT id, org_id, doc_id, parent_id, chunk_type,
                           text, breadcrumb, metadata
                    FROM policy_chunks
                    WHERE org_id = :org_id AND chunk_type = 'parent'
                    """
                ),
                {"org_id": org_id},
            ).fetchall()
            if not rows:
                rows = connection.execute(
                    text(
                        """
                        SELECT id, org_id, doc_id, parent_id, chunk_type,
                               text, breadcrumb, metadata
                        FROM policy_chunks WHERE org_id = :org_id
                        """
                    ),
                    {"org_id": org_id},
                ).fetchall()
        return [_row_to_chunk(row) for row in rows]

    def _get_bm25(self, org_id: str) -> tuple[Optional[BM25Okapi], list[Chunk]]:
        if org_id in self._bm25_indices:
            return self._bm25_indices[org_id]
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT id, org_id, doc_id, parent_id, chunk_type,
                           text, breadcrumb, metadata
                    FROM policy_chunks
                    WHERE org_id = :org_id AND chunk_type = 'child'
                    """
                ),
                {"org_id": org_id},
            ).fetchall()
        if not rows:
            return None, []
        chunks = [_row_to_chunk(row) for row in rows]
        from core.bm25_index import build_index

        bm25 = build_index(chunks)
        if bm25 is None:
            return None, []
        self._bm25_indices[org_id] = (bm25, chunks)
        return bm25, chunks
