"""The job table is the queue.

A row holds the job id, the company, and the object keys. It does not hold
the PDF. One worker claims a queued row. A later Kafka consumer can read the
same kind names.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

POLICY_UPLOADED = "policy.uploaded"
CONTRACT_SUBMITTED = "contract.submitted"

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


def _sqlite_path() -> str:
    return os.getenv(
        "SQLITE_DB_PATH",
        str(Path(__file__).resolve().parents[1] / "data" / "contract_checker.db"),
    )


def _uses_postgres() -> bool:
    return os.getenv("VECTOR_STORE_BACKEND", "sqlite").lower() == "pgvector"


def _loads(raw: str | None):
    if not raw:
        return None
    return json.loads(raw)


def _public(row: dict) -> dict:
    files = row["files"] if isinstance(row["files"], list) else _loads(row["files"]) or []
    result = row["result"] if isinstance(row["result"], (dict, list)) else _loads(row["result"])
    return {
        "job_id": row["job_id"],
        "org_id": row["org_id"],
        "kind": row["kind"],
        "status": row["status"],
        "object_keys": [item["object_key"] for item in files],
        "error": row["error"],
        "result": result,
    }


class _Engine:
    """One short-lived Postgres engine. It is closed before the caller returns."""

    def __enter__(self):
        from sqlalchemy import create_engine

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not set.")
        self.engine = create_engine(database_url)
        return self.engine

    def __exit__(self, exc_type, exc, traceback):
        self.engine.dispose()


def ensure_jobs_table() -> None:
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(872364)"))
            connection.execute(text(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id     TEXT PRIMARY KEY,
                    org_id     TEXT NOT NULL,
                    kind       TEXT NOT NULL,
                    status     TEXT NOT NULL,
                    files      TEXT NOT NULL,
                    result     TEXT,
                    error      TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs (status, created_at)"
            ))
        return

    db_path = _sqlite_path()
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id     TEXT PRIMARY KEY,
                org_id     TEXT NOT NULL,
                kind       TEXT NOT NULL,
                status     TEXT NOT NULL,
                files      TEXT NOT NULL,
                result     TEXT,
                error      TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs (status, created_at)"
        )
        connection.commit()
    finally:
        connection.close()


def create_job(org_id: str, kind: str, files: list[dict]) -> str:
    """Insert a queued job. files are keys and names, not PDF bytes."""
    ensure_jobs_table()
    job_id = str(uuid.uuid4())
    payload = json.dumps(files)
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO jobs (job_id, org_id, kind, status, files)
                    VALUES (:job_id, :org_id, :kind, :status, :files)
                    """
                ),
                {
                    "job_id": job_id,
                    "org_id": org_id,
                    "kind": kind,
                    "status": QUEUED,
                    "files": payload,
                },
            )
        return job_id

    connection = sqlite3.connect(_sqlite_path())
    try:
        connection.execute(
            """
            INSERT INTO jobs (job_id, org_id, kind, status, files)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, org_id, kind, QUEUED, payload),
        )
        connection.commit()
    finally:
        connection.close()
    return job_id


def get_job(org_id: str, job_id: str) -> dict | None:
    ensure_jobs_table()
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT job_id, org_id, kind, status, files, result, error
                    FROM jobs
                    WHERE job_id = :job_id AND org_id = :org_id
                    """
                ),
                {"job_id": job_id, "org_id": org_id},
            ).mappings().first()
        if row is None:
            return None
        return _public(dict(row))

    connection = sqlite3.connect(_sqlite_path())
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT job_id, org_id, kind, status, files, result, error
            FROM jobs
            WHERE job_id = ? AND org_id = ?
            """,
            (job_id, org_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return _public(dict(row))


def claim_next() -> dict | None:
    """Mark one queued job running and return it. A second worker skips that row."""
    ensure_jobs_table()
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            row = connection.execute(text(
                """
                WITH next_job AS (
                    SELECT job_id
                    FROM jobs
                    WHERE status = 'queued'
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE jobs
                SET status = 'running', updated_at = now()
                FROM next_job
                WHERE jobs.job_id = next_job.job_id
                RETURNING jobs.job_id, jobs.org_id, jobs.kind, jobs.files
                """
            )).mappings().first()
        if row is None:
            return None
        claimed = dict(row)
        claimed["files"] = _loads(claimed["files"]) or []
        return claimed

    connection = sqlite3.connect(_sqlite_path())
    connection.isolation_level = None
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT job_id, org_id, kind, files
            FROM jobs
            WHERE status = 'queued'
            ORDER BY created_at
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            connection.execute("COMMIT")
            return None
        updated = connection.execute(
            """
            UPDATE jobs
            SET status = 'running', updated_at = datetime('now')
            WHERE job_id = ? AND status = 'queued'
            """,
            (row["job_id"],),
        )
        connection.execute("COMMIT")
        if updated.rowcount != 1:
            return None
        claimed = dict(row)
        claimed["files"] = _loads(claimed["files"]) or []
        return claimed
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def mark_succeeded(job_id: str, result: dict) -> None:
    _finish(job_id, SUCCEEDED, json.dumps(result), None)


def mark_failed(job_id: str, error: str) -> None:
    _finish(job_id, FAILED, None, error)


def _finish(job_id: str, status: str, result: str | None, error: str | None) -> None:
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = :status, result = :result, error = :error, updated_at = now()
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id, "status": status, "result": result, "error": error},
            )
        return

    connection = sqlite3.connect(_sqlite_path())
    try:
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, result = ?, error = ?, updated_at = datetime('now')
            WHERE job_id = ?
            """,
            (status, result, error, job_id),
        )
        connection.commit()
    finally:
        connection.close()
