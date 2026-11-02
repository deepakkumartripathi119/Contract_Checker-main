"""Row for an uploaded file. The bytes are not in this table."""

from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

from core.object_store import ObjectStore, get_object_store


_CREATE = """
CREATE TABLE IF NOT EXISTS stored_documents (
    document_id TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    filename    TEXT NOT NULL,
    object_key  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _safe_filename(name: str) -> str:
    base = Path(name).name
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in base)
    return cleaned or "document.pdf"


def _sqlite_path() -> str:
    return os.getenv(
        "SQLITE_DB_PATH",
        str(Path(__file__).resolve().parents[1] / "data" / "contract_checker.db"),
    )


def _uses_postgres() -> bool:
    return os.getenv("VECTOR_STORE_BACKEND", "sqlite").lower() == "pgvector"


def ensure_document_table() -> None:
    """Create the key table if this database does not have it yet."""
    if _uses_postgres():
        from sqlalchemy import create_engine, text

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not set.")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(text("SELECT pg_advisory_xact_lock(872364)"))
                connection.execute(text(
                    """
                    CREATE TABLE IF NOT EXISTS stored_documents (
                        document_id TEXT PRIMARY KEY,
                        org_id      TEXT NOT NULL,
                        kind        TEXT NOT NULL,
                        filename    TEXT NOT NULL,
                        object_key  TEXT NOT NULL,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                ))
        finally:
            engine.dispose()
        return

    db_path = _sqlite_path()
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_CREATE)
        connection.commit()
    finally:
        connection.close()


def _save_sqlite(document_id: str, org_id: str, kind: str, filename: str, object_key: str) -> None:
    ensure_document_table()
    connection = sqlite3.connect(_sqlite_path())
    try:
        connection.execute(
            """
            INSERT INTO stored_documents (document_id, org_id, kind, filename, object_key)
            VALUES (?, ?, ?, ?, ?)
            """,
            (document_id, org_id, kind, filename, object_key),
        )
        connection.commit()
    finally:
        connection.close()


def _save_postgres(document_id: str, org_id: str, kind: str, filename: str, object_key: str) -> None:
    from sqlalchemy import create_engine, text

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set.")
    engine = create_engine(database_url)
    try:
        ensure_document_table()
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO stored_documents (document_id, org_id, kind, filename, object_key)
                    VALUES (:document_id, :org_id, :kind, :filename, :object_key)
                    """
                ),
                {
                    "document_id": document_id,
                    "org_id": org_id,
                    "kind": kind,
                    "filename": filename,
                    "object_key": object_key,
                },
            )
    finally:
        engine.dispose()


def keep_original_file(org_id: str, kind: str, filename: str, data: bytes, store: ObjectStore | None = None) -> dict:
    """Save the original bytes and one row that points at them."""
    document_id = str(uuid.uuid4())
    object_key = f"{org_id}/{kind}/{document_id}/{_safe_filename(filename)}"
    target = store or get_object_store()
    target.put(object_key, data)
    if _uses_postgres():
        _save_postgres(document_id, org_id, kind, filename, object_key)
    else:
        _save_sqlite(document_id, org_id, kind, filename, object_key)
    return {
        "document_id": document_id,
        "object_key": object_key,
        "filename": filename,
    }
