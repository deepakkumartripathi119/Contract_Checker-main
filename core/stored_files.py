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


def stage_original_file(
    org_id: str,
    kind: str,
    filename: str,
    data: bytes,
    store: ObjectStore | None = None,
) -> dict:
    """Save the bytes. The database row is written by the caller's transaction."""
    document_id = str(uuid.uuid4())
    object_key = f"{org_id}/{kind}/{document_id}/{_safe_filename(filename)}"
    target = store or get_object_store()
    target.put(object_key, data)
    return {
        "document_id": document_id,
        "object_key": object_key,
        "filename": filename,
    }


def insert_document_on(connection, postgres: bool, org_id: str, kind: str, item: dict) -> None:
    if postgres:
        from sqlalchemy import text

        connection.execute(
            text(
                """
                INSERT INTO stored_documents (document_id, org_id, kind, filename, object_key)
                VALUES (:document_id, :org_id, :kind, :filename, :object_key)
                """
            ),
            {
                "document_id": item["document_id"],
                "org_id": org_id,
                "kind": kind,
                "filename": item["filename"],
                "object_key": item["object_key"],
            },
        )
        return
    connection.execute(
        """
        INSERT INTO stored_documents (document_id, org_id, kind, filename, object_key)
        VALUES (?, ?, ?, ?, ?)
        """,
        (item["document_id"], org_id, kind, item["filename"], item["object_key"]),
    )


def record_documents(org_id: str, kind: str, staged: list[dict]) -> None:
    """Insert these file rows in one commit."""
    if not staged:
        return
    ensure_document_table()
    from core.jobs import _in_transaction

    def write(connection, postgres: bool) -> None:
        for item in staged:
            insert_document_on(connection, postgres, org_id, kind, item)

    _in_transaction(write)


def keep_original_file(org_id: str, kind: str, filename: str, data: bytes, store: ObjectStore | None = None) -> dict:
    """Save the original bytes and one row that points at them."""
    staged = stage_original_file(org_id, kind, filename, data, store)
    if _uses_postgres():
        _save_postgres(
            staged["document_id"], org_id, kind, staged["filename"], staged["object_key"]
        )
    else:
        _save_sqlite(
            staged["document_id"], org_id, kind, staged["filename"], staged["object_key"]
        )
    return staged


def keep_only(org_id: str, kind: str, keep_keys: set[str]) -> None:
    """Delete this company's other files of this kind. The kept keys stay."""
    ensure_document_table()
    rows = _rows(org_id, kind)
    store = get_object_store()
    removed = []
    for row in rows:
        if row["object_key"] in keep_keys:
            continue
        try:
            store.delete(row["object_key"])
        except Exception:
            pass
        removed.append(row["document_id"])
    _delete_document_ids(removed)


def _rows(org_id: str, kind: str) -> list[dict]:
    if _uses_postgres():
        from sqlalchemy import create_engine, text

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not set.")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                found = connection.execute(
                    text(
                        """
                        SELECT document_id, object_key
                        FROM stored_documents
                        WHERE org_id = :org_id AND kind = :kind
                        """
                    ),
                    {"org_id": org_id, "kind": kind},
                ).mappings().all()
            return [dict(row) for row in found]
        finally:
            engine.dispose()
    connection = sqlite3.connect(_sqlite_path())
    connection.row_factory = sqlite3.Row
    try:
        found = connection.execute(
            """
            SELECT document_id, object_key
            FROM stored_documents
            WHERE org_id = ? AND kind = ?
            """,
            (org_id, kind),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in found]


def discard_object_bytes(org_id: str, keys: list[str]) -> list[str]:
    """Delete the bytes. The caller deletes the rows in its own transaction."""
    wanted = {key for key in keys if key}
    if not wanted:
        return []
    rows = []
    for kind in ("contracts", "policies"):
        rows.extend(_rows(org_id, kind))
    known = set()
    document_ids = []
    store = get_object_store()
    for row in rows:
        if row["object_key"] not in wanted:
            continue
        known.add(row["object_key"])
        document_ids.append(row["document_id"])
        try:
            store.delete(row["object_key"])
        except Exception:
            pass
    for key in wanted - known:
        try:
            store.delete(key)
        except Exception:
            pass
    return document_ids


def delete_object_keys(org_id: str, keys: list[str]) -> None:
    """Delete these stored files. A missing key is left alone."""
    _delete_document_ids(discard_object_bytes(org_id, keys))


def _delete_document_ids(document_ids: list[str]) -> None:
    if not document_ids:
        return
    ensure_document_table()
    if _uses_postgres():
        from sqlalchemy import create_engine, text

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not set.")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                for document_id in document_ids:
                    connection.execute(
                        text("DELETE FROM stored_documents WHERE document_id = :document_id"),
                        {"document_id": document_id},
                    )
        finally:
            engine.dispose()
        return
    connection = sqlite3.connect(_sqlite_path())
    try:
        for document_id in document_ids:
            connection.execute(
                "DELETE FROM stored_documents WHERE document_id = ?",
                (document_id,),
            )
        connection.commit()
    finally:
        connection.close()
