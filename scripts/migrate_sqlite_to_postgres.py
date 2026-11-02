"""Read a SQLite backup and, only when asked, copy it into Postgres.

The default is a dry run. It does not open the source database for writing
and it does not copy compliance-cache rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.pg_schema import EMBEDDING_DIMENSIONS

EMBEDDING_BYTES = EMBEDDING_DIMENSIONS * 4


def decode_embedding(blob: bytes | None, chunk_type: str) -> list[float] | None:
    if blob is None:
        if chunk_type != "parent":
            raise ValueError("A child chunk is missing its embedding.")
        return None
    if len(blob) != EMBEDDING_BYTES:
        raise ValueError(
            f"Embedding is {len(blob)} bytes. Expected {EMBEDDING_BYTES} "
            f"for {EMBEDDING_DIMENSIONS} float32 values."
        )
    return np.frombuffer(blob, dtype="<f4").astype(np.float64).tolist()


def inspect_sqlite(path: Path) -> dict:
    """Read a backup and validate it. The connection is read-only."""
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        chunks = []
        for row in connection.execute("SELECT * FROM chunks ORDER BY chunk_type DESC, id"):
            metadata = json.loads(row["metadata"])
            embedding = decode_embedding(row["embedding"], row["chunk_type"])
            chunks.append({
                "id": row["id"],
                "org_id": row["org_id"],
                "doc_id": row["doc_id"],
                "parent_id": row["parent_id"],
                "chunk_type": row["chunk_type"],
                "text": row["text"],
                "breadcrumb": row["breadcrumb"],
                "metadata": metadata,
                "embedding": embedding,
            })
        parent_ids = {chunk["id"] for chunk in chunks if chunk["chunk_type"] == "parent"}
        orphans = [
            chunk["id"]
            for chunk in chunks
            if chunk["parent_id"] and chunk["parent_id"] not in parent_ids
        ]
        if orphans:
            raise ValueError(f"Child chunks point at missing parents: {orphans}")
        tenants = []
        for row in connection.execute("SELECT * FROM tenant_configs"):
            tenants.append({
                "org_id": row["org_id"],
                "name": row["name"],
                "aliases": json.loads(row["aliases"]),
                "active_policy_version": row["active_policy_version"],
                "silent_policy_behavior": row["silent_policy_behavior"],
                "favorable_deviation_behavior": row["favorable_deviation_behavior"],
                "supported_languages": json.loads(row["supported_languages"]),
                "updated_at": row["updated_at"],
            })
        cache_count = connection.execute(
            "SELECT COUNT(*) FROM compliance_cache"
        ).fetchone()[0]
    finally:
        connection.close()
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    if before != after:
        raise RuntimeError("The SQLite file changed while it was being read.")
    children = [chunk for chunk in chunks if chunk["chunk_type"] == "child"]
    parents = [chunk for chunk in chunks if chunk["chunk_type"] == "parent"]
    return {
        "source_sha256": before,
        "chunks": chunks,
        "chunk_count": len(chunks),
        "parent_count": len(parents),
        "child_count": len(children),
        "child_embedding_count": sum(chunk["embedding"] is not None for chunk in children),
        "embedding_dim": EMBEDDING_DIMENSIONS,
        "orphan_count": 0,
        "tenants": tenants,
        "cache_rows_not_migrated": cache_count,
    }


def apply_to_postgres(report: dict, database_url: str) -> None:
    from sqlalchemy import create_engine, text

    from core.pg_schema import apply_schema
    from core.pg_vector_store import _vector_literal

    engine = create_engine(database_url)
    parents = [chunk for chunk in report["chunks"] if chunk["parent_id"] is None]
    children = [chunk for chunk in report["chunks"] if chunk["parent_id"] is not None]
    with engine.begin() as connection:
        apply_schema(connection)
        for chunk in [*parents, *children]:
            embedding = (
                _vector_literal(chunk["embedding"]) if chunk["embedding"] else None
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
                    "id": chunk["id"],
                    "org_id": chunk["org_id"],
                    "doc_id": chunk["doc_id"],
                    "parent_id": chunk["parent_id"],
                    "chunk_type": chunk["chunk_type"],
                    "text": chunk["text"],
                    "breadcrumb": chunk["breadcrumb"],
                    "metadata": json.dumps(chunk["metadata"]),
                    "embedding": embedding,
                },
            )
        for tenant in report["tenants"]:
            updated = datetime.strptime(
                tenant["updated_at"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            connection.execute(
                text(
                    """
                    INSERT INTO tenant_configs (
                        org_id, name, aliases, active_policy_version,
                        silent_policy_behavior, favorable_deviation_behavior,
                        supported_languages, updated_at
                    ) VALUES (
                        :org_id, :name, CAST(:aliases AS jsonb), :version,
                        :silent, :favorable, CAST(:languages AS jsonb), :updated_at
                    )
                    ON CONFLICT (org_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        aliases = EXCLUDED.aliases,
                        active_policy_version = EXCLUDED.active_policy_version,
                        silent_policy_behavior = EXCLUDED.silent_policy_behavior,
                        favorable_deviation_behavior = EXCLUDED.favorable_deviation_behavior,
                        supported_languages = EXCLUDED.supported_languages,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "org_id": tenant["org_id"],
                    "name": tenant["name"],
                    "aliases": json.dumps(tenant["aliases"]),
                    "version": tenant["active_policy_version"],
                    "silent": tenant["silent_policy_behavior"],
                    "favorable": tenant["favorable_deviation_behavior"],
                    "languages": json.dumps(tenant["supported_languages"]),
                    "updated_at": updated,
                },
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        default="data/contract_checker.phase3.bak",
        help="SQLite backup to read. The original database is not the default.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to DATABASE_URL. Without this flag, nothing is written.",
    )
    args = parser.parse_args()
    source = Path(args.source)
    report = inspect_sqlite(source)
    print(
        f"Read {report['chunk_count']} chunks "
        f"({report['parent_count']} parents, {report['child_count']} children), "
        f"{len(report['tenants'])} tenants. "
        f"Leaving {report['cache_rows_not_migrated']} cache rows behind."
    )
    print(f"Source SHA-256 unchanged: {report['source_sha256']}")
    if not args.apply:
        print("Dry run only. No Postgres write.")
        return
    import os
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required for --apply.")
    apply_to_postgres(report, database_url)
    print("Postgres write finished. Cache was not copied.")


if __name__ == "__main__":
    main()
