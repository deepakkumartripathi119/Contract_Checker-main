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
CLAUSE_SUBMITTED = "clause.submitted"
VENDOR_DELETED = "vendor.deleted"
REPORT_DELETED = "report.deleted"

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
DELETING = "deleting"

EXTRACTING = "extracting"
ANALYSING = "analysing"
MATCHING = "matching"
COMPLETED = "completed"
CONTRACT_STEPS = (EXTRACTING, ANALYSING, MATCHING, COMPLETED)


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
        "filename": files[0]["filename"] if files else "",
        "object_keys": [item["object_key"] for item in files],
        "error": row["error"],
        "result": result,
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "step": row.get("step") or "",
    }


def _iso(value):
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


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
            connection.execute(text(
                "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS step TEXT NOT NULL DEFAULT ''"
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
        columns = [row[1] for row in connection.execute("PRAGMA table_info(jobs)")]
        if "step" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN step TEXT NOT NULL DEFAULT ''")
        connection.commit()
    finally:
        connection.close()


def create_job(
    org_id: str,
    kind: str,
    files: list[dict],
    status: str = QUEUED,
    result: dict | None = None,
) -> str:
    """Insert a job. files are keys and names, not PDF bytes."""
    ensure_jobs_table()
    job_id = str(uuid.uuid4())
    payload = json.dumps(files)
    stored = json.dumps(result) if result is not None else None
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO jobs (job_id, org_id, kind, status, files, result)
                    VALUES (:job_id, :org_id, :kind, :status, :files, :result)
                    """
                ),
                {
                    "job_id": job_id,
                    "org_id": org_id,
                    "kind": kind,
                    "status": status,
                    "files": payload,
                    "result": stored,
                },
            )
        return job_id

    connection = sqlite3.connect(_sqlite_path())
    try:
        connection.execute(
            """
            INSERT INTO jobs (job_id, org_id, kind, status, files, result)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, org_id, kind, status, payload, stored),
        )
        connection.commit()
    finally:
        connection.close()
    return job_id


def list_contract_jobs(org_id: str) -> list[dict]:
    """Every contract check for one company, newest first."""
    return [_contract_row(row) for row in _contract_rows(org_id)]


def _contract_rows(org_id: str) -> list[dict]:
    """Every contract check, newest first."""
    ensure_jobs_table()
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT job_id, status, files, created_at
                    FROM jobs
                    WHERE org_id = :org_id AND kind = :kind
                      AND status <> 'deleting'
                    ORDER BY created_at DESC
                    """
                ),
                {"org_id": org_id, "kind": CONTRACT_SUBMITTED},
            ).mappings().all()
        return [dict(row) for row in rows]

    connection = sqlite3.connect(_sqlite_path())
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT job_id, status, files, created_at
            FROM jobs
            WHERE org_id = ? AND kind = ?
              AND status <> 'deleting'
            ORDER BY created_at DESC
            """,
            (org_id, CONTRACT_SUBMITTED),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _file_digest(row: dict) -> str:
    """Same bytes share one row. A missing file stays as its own row."""
    files = _loads(row["files"]) or []
    if not files:
        return row["job_id"]
    saved = files[0].get("file_hash")
    if isinstance(saved, str) and saved:
        return saved
    try:
        from core.cache import file_hash
        from core.object_store import get_object_store
        return file_hash(get_object_store().get(files[0]["object_key"]))
    except Exception:
        return row["job_id"]


def _contract_row(row: dict) -> dict:
    files = _loads(row["files"]) or []
    created = row["created_at"]
    return {
        "job_id": row["job_id"],
        "filename": files[0]["filename"] if files else "",
        "status": row["status"],
        "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created),
    }


def get_job(org_id: str, job_id: str) -> dict | None:
    ensure_jobs_table()
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT job_id, org_id, kind, status, step, files, result, error, created_at, updated_at
                    FROM jobs
                    WHERE job_id = :job_id AND org_id = :org_id
                      AND status <> 'deleting'
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
            SELECT job_id, org_id, kind, status, step, files, result, error, created_at, updated_at
            FROM jobs
            WHERE job_id = ? AND org_id = ?
              AND status <> 'deleting'
            """,
            (job_id, org_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return _public(dict(row))


def _lease_seconds() -> int:
    """A running job older than this can be claimed again. A live check is shorter."""
    return int(os.getenv("JOB_LEASE_SECONDS", "900"))


def claim_next() -> dict | None:
    """Mark one job running. A fresh running row is skipped. A stale one is taken again."""
    ensure_jobs_table()
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    WITH next_job AS (
                        SELECT job_id
                        FROM jobs
                        WHERE status = 'queued'
                           OR (status = 'running' AND updated_at < now() - make_interval(secs => :lease))
                        ORDER BY CASE WHEN status = 'queued' THEN 0 ELSE 1 END, created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE jobs
                    SET status = 'running', updated_at = now()
                    FROM next_job
                    WHERE jobs.job_id = next_job.job_id
                    RETURNING jobs.job_id, jobs.org_id, jobs.kind, jobs.files
                    """
                ),
                {"lease": _lease_seconds()},
            ).mappings().first()
        if row is None:
            return None
        claimed = dict(row)
        claimed["files"] = _loads(claimed["files"]) or []
        return claimed

    connection = sqlite3.connect(_sqlite_path())
    connection.isolation_level = None
    connection.row_factory = sqlite3.Row
    modifier = f"-{_lease_seconds()} seconds"
    try:
        connection.execute("BEGIN IMMEDIATE")
        for _ in range(5):
            row = connection.execute(
                """
                SELECT job_id, org_id, kind, files
                FROM jobs
                WHERE status = 'queued'
                   OR (status = 'running' AND updated_at < datetime('now', ?))
                ORDER BY CASE WHEN status = 'queued' THEN 0 ELSE 1 END, created_at
                LIMIT 1
                """,
                (modifier,),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'running', updated_at = datetime('now')
                WHERE job_id = ?
                  AND (
                    status = 'queued'
                    OR (status = 'running' AND updated_at < datetime('now', ?))
                  )
                """,
                (row["job_id"], modifier),
            )
            if updated.rowcount == 1:
                connection.execute("COMMIT")
                claimed = dict(row)
                claimed["files"] = _loads(claimed["files"]) or []
                return claimed
        connection.execute("COMMIT")
        return None
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _in_transaction(write) -> None:
    """Run one database change. A crash or an error rolls every statement back."""
    if _uses_postgres():
        with _Engine() as engine, engine.begin() as connection:
            write(connection, True)
        return
    connection = sqlite3.connect(_sqlite_path())
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        write(connection, False)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _finish_on(connection, postgres: bool, job_id: str, status: str, result, error, step) -> None:
    if postgres:
        from sqlalchemy import text

        assignment = "status = :status, result = :result, error = :error, updated_at = now()"
        values = {"job_id": job_id, "status": status, "result": result, "error": error}
        if step is not None:
            assignment += ", step = :step"
            values["step"] = step
        connection.execute(
            text(f"UPDATE jobs SET {assignment} WHERE job_id = :job_id AND status <> 'deleting'"),
            values,
        )
        return
    if step is None:
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, result = ?, error = ?, updated_at = datetime('now')
            WHERE job_id = ? AND status <> 'deleting'
            """,
            (status, result, error, job_id),
        )
        return
    connection.execute(
        """
        UPDATE jobs
        SET status = ?, result = ?, error = ?, step = ?, updated_at = datetime('now')
        WHERE job_id = ? AND status <> 'deleting'
        """,
        (status, result, error, step, job_id),
    )


def mark_succeeded(job_id: str, result: dict) -> None:
    """Store the report and move a waiting vendor in the same commit."""
    from core.vendors import ensure_vendors_table, settle_pending_on

    ensure_vendors_table()
    payload = json.dumps(result)

    def write(connection, postgres: bool) -> None:
        _finish_on(connection, postgres, job_id, SUCCEEDED, payload, None, COMPLETED)
        settle_pending_on(connection, postgres, job_id)

    _in_transaction(write)


def delete_job(org_id: str, job_id: str) -> None:
    """Remove one report row and the file it points at."""
    job = get_job(org_id, job_id)
    if job is None:
        return
    from core.object_store import get_object_store

    store = get_object_store()
    for key in job.get("object_keys") or []:
        if not key:
            continue
        try:
            store.delete(key)
        except Exception:
            pass
    ensure_jobs_table()
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            connection.execute(
                text("DELETE FROM jobs WHERE job_id = :job_id AND org_id = :org_id"),
                {"job_id": job_id, "org_id": org_id},
            )
        return
    connection = sqlite3.connect(_sqlite_path())
    try:
        connection.execute(
            "DELETE FROM jobs WHERE job_id = ? AND org_id = ?",
            (job_id, org_id),
        )
        connection.commit()
    finally:
        connection.close()


def current_policy(org_id: str) -> dict:
    """The last succeeded policy file, plus a replacement that is not finished yet."""
    ensure_jobs_table()
    rows = _policy_rows(org_id)
    pending = None
    current = None
    for row in rows:
        if pending is None and row["status"] in (QUEUED, RUNNING):
            pending = row["status"]
        if current is None and row["status"] == SUCCEEDED:
            files = _loads(row["files"]) or []
            current = {
                "filename": files[0]["filename"] if files else "",
                "object_key": files[0]["object_key"] if files else "",
                "job_id": row["job_id"],
            }
    return {"file": current, "pending_status": pending or ""}


def _policy_rows(org_id: str) -> list[dict]:
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            found = connection.execute(
                text(
                    """
                    SELECT job_id, status, files, created_at
                    FROM jobs
                    WHERE org_id = :org_id AND kind = :kind
                      AND status <> 'deleting'
                    ORDER BY created_at DESC
                    """
                ),
                {"org_id": org_id, "kind": POLICY_UPLOADED},
            ).mappings().all()
        return [dict(row) for row in found]
    connection = sqlite3.connect(_sqlite_path())
    connection.row_factory = sqlite3.Row
    try:
        found = connection.execute(
            """
            SELECT job_id, status, files, created_at
            FROM jobs
            WHERE org_id = ? AND kind = ?
              AND status <> 'deleting'
            ORDER BY created_at DESC
            """,
            (org_id, POLICY_UPLOADED),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in found]


def set_job_step(job_id: str, step: str) -> None:
    """Record the contract step that is starting. The status stays running."""
    if step not in CONTRACT_STEPS:
        raise ValueError("Unknown contract step")
    ensure_jobs_table()
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE jobs
                    SET step = :step, updated_at = now()
                    WHERE job_id = :job_id AND status <> 'deleting'
                    """
                ),
                {"job_id": job_id, "step": step},
            )
        return

    connection = sqlite3.connect(_sqlite_path())
    try:
        connection.execute(
            """
            UPDATE jobs
            SET step = ?, updated_at = datetime('now')
            WHERE job_id = ? AND status <> 'deleting'
            """,
            (step, job_id),
        )
        connection.commit()
    finally:
        connection.close()


def mark_failed(job_id: str, error: str) -> None:
    _finish(job_id, FAILED, None, error)


def _finish(job_id: str, status: str, result: str | None, error: str | None, step: str | None = None) -> None:
    _in_transaction(
        lambda connection, postgres: _finish_on(
            connection, postgres, job_id, status, result, error, step
        )
    )


def _raw_job(org_id: str, job_id: str) -> dict | None:
    """The row even when it is waiting to be erased. Readers use get_job."""
    if not job_id:
        return None
    ensure_jobs_table()
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT job_id, files
                    FROM jobs
                    WHERE job_id = :job_id AND org_id = :org_id
                    """
                ),
                {"job_id": job_id, "org_id": org_id},
            ).mappings().first()
        return dict(row) if row else None
    connection = sqlite3.connect(_sqlite_path())
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT job_id, files FROM jobs WHERE job_id = ? AND org_id = ?",
            (job_id, org_id),
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row else None


def _vendor_change(
    connection,
    change: str,
    org_id: str,
    vendor_id: str,
    *,
    postgres: bool,
    adopt_job_id: str = "",
    adopt_mode: str = "",
    expected_job_id: str = "",
) -> int:
    """Apply the vendor write on the same connection as the other statements."""
    if not change:
        return 1
    if postgres:
        from sqlalchemy import text

        if change == "remove":
            statement = "DELETE FROM vendors WHERE vendor_id = :vendor_id AND org_id = :org_id"
        elif change == "clear":
            statement = """
                UPDATE vendors
                SET job_id = NULL, input_mode = NULL, pending_job_id = NULL
                WHERE vendor_id = :vendor_id AND org_id = :org_id
                """
        elif change == "adopt":
            statement = """
                UPDATE vendors
                SET job_id = :adopt_job_id, pending_job_id = NULL, input_mode = :adopt_mode,
                    mode_claimed_at = NULL
                WHERE vendor_id = :vendor_id AND org_id = :org_id
                  AND pending_job_id = :adopt_job_id
                """
        elif change == "replace":
            statement = """
                UPDATE vendors
                SET job_id = :adopt_job_id, pending_job_id = NULL, input_mode = :adopt_mode,
                    mode_claimed_at = NULL
                WHERE vendor_id = :vendor_id AND org_id = :org_id
                  AND job_id = :expected_job_id
                  AND (pending_job_id IS NULL OR pending_job_id = '')
                """
        else:
            raise ValueError(change)
        result = connection.execute(
            text(statement),
            {
                "vendor_id": vendor_id,
                "org_id": org_id,
                "adopt_job_id": adopt_job_id,
                "adopt_mode": adopt_mode,
                "expected_job_id": expected_job_id,
            },
        )
        return result.rowcount
    if change == "remove":
        statement = "DELETE FROM vendors WHERE vendor_id = ? AND org_id = ?"
        params = (vendor_id, org_id)
    elif change == "clear":
        statement = """
            UPDATE vendors
            SET job_id = NULL, input_mode = NULL, pending_job_id = NULL
            WHERE vendor_id = ? AND org_id = ?
            """
        params = (vendor_id, org_id)
    elif change == "adopt":
        statement = """
            UPDATE vendors
            SET job_id = ?, pending_job_id = NULL, input_mode = ?, mode_claimed_at = NULL
            WHERE vendor_id = ? AND org_id = ? AND pending_job_id = ?
            """
        params = (adopt_job_id, adopt_mode, vendor_id, org_id, adopt_job_id)
    elif change == "replace":
        statement = """
            UPDATE vendors
            SET job_id = ?, pending_job_id = NULL, input_mode = ?, mode_claimed_at = NULL
            WHERE vendor_id = ? AND org_id = ? AND job_id = ?
              AND (pending_job_id IS NULL OR pending_job_id = '')
            """
        params = (adopt_job_id, adopt_mode, vendor_id, org_id, expected_job_id)
    else:
        raise ValueError(change)
    return connection.execute(statement, params).rowcount


def queue_delete(
    org_id: str,
    kind: str,
    vendor_id: str,
    job_ids: list[str],
    vendor_change: str = "",
    adopt_job_id: str = "",
    adopt_mode: str = "",
    expected_job_id: str = "",
    connection=None,
    postgres: bool | None = None,
) -> str:
    """Hide these jobs and leave one event that erases them.

    The event kind is the name a later Kafka consumer will read. Until that
    consumer runs, get_job and the worker claim skip a row in `deleting`, and
    a finish write cannot put it back. The job update, the event, and the
    vendor write commit in one transaction. A crash before that commit rolls
    all three back.
    """
    holder = {"event_id": ""}

    def write(target, uses_postgres: bool) -> None:
        ids, keys, hashes = _job_files_on(target, uses_postgres, org_id, job_ids)
        event_id = str(uuid.uuid4())
        holder["event_id"] = event_id
        payload = json.dumps([{
            "filename": kind,
            "object_key": "",
            "vendor_id": vendor_id,
            "job_ids": ids,
            "object_keys": keys,
            "file_hashes": hashes,
        }])
        _write_delete(
            target,
            uses_postgres,
            org_id,
            ids,
            event_id,
            kind,
            payload,
            vendor_id,
            vendor_change,
            adopt_job_id,
            adopt_mode,
            expected_job_id,
        )

    if connection is not None:
        write(connection, bool(postgres))
        return holder["event_id"]
    ensure_jobs_table()
    _in_transaction(write)
    return holder["event_id"]


def _job_files_on(connection, postgres: bool, org_id: str, job_ids: list[str]) -> tuple[list[str], list[str], list[str]]:
    ids = []
    keys = []
    hashes = []
    for job_id in job_ids:
        if not job_id or job_id in ids:
            continue
        if postgres:
            from sqlalchemy import text

            row = connection.execute(
                text("SELECT job_id, files FROM jobs WHERE job_id = :job_id AND org_id = :org_id"),
                {"job_id": job_id, "org_id": org_id},
            ).mappings().first()
        else:
            row = connection.execute(
                "SELECT job_id, files FROM jobs WHERE job_id = ? AND org_id = ?",
                (job_id, org_id),
            ).fetchone()
        if row is None:
            continue
        ids.append(row["job_id"])
        for item in _loads(row["files"]) or []:
            if item.get("object_key"):
                keys.append(item["object_key"])
            if item.get("file_hash"):
                hashes.append(item["file_hash"])
    return ids, keys, hashes


def _write_delete(
    connection,
    postgres: bool,
    org_id: str,
    ids: list[str],
    event_id: str,
    kind: str,
    payload: str,
    vendor_id: str,
    vendor_change: str,
    adopt_job_id: str,
    adopt_mode: str,
    expected_job_id: str,
) -> None:
    if postgres:
        from sqlalchemy import text

        for job_id in ids:
            connection.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = 'deleting', updated_at = now()
                    WHERE job_id = :job_id AND org_id = :org_id
                      AND status <> 'deleting'
                    """
                ),
                {"job_id": job_id, "org_id": org_id},
            )
        connection.execute(
            text(
                """
                INSERT INTO jobs (job_id, org_id, kind, status, files)
                VALUES (:job_id, :org_id, :kind, 'queued', :files)
                """
            ),
            {"job_id": event_id, "org_id": org_id, "kind": kind, "files": payload},
        )
    else:
        for job_id in ids:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'deleting', updated_at = datetime('now')
                WHERE job_id = ? AND org_id = ? AND status <> 'deleting'
                """,
                (job_id, org_id),
            )
        connection.execute(
            """
            INSERT INTO jobs (job_id, org_id, kind, status, files)
            VALUES (?, ?, ?, 'queued', ?)
            """,
            (event_id, org_id, kind, payload),
        )
    changed = _vendor_change(
        connection,
        vendor_change,
        org_id,
        vendor_id,
        postgres=postgres,
        adopt_job_id=adopt_job_id,
        adopt_mode=adopt_mode,
        expected_job_id=expected_job_id,
    )
    if vendor_change and changed == 0:
        raise LookupError(vendor_id)


def insert_job_on(
    connection,
    postgres: bool,
    job_id: str,
    org_id: str,
    kind: str,
    status: str,
    files_json: str,
    result_json: str | None,
) -> None:
    if postgres:
        from sqlalchemy import text

        connection.execute(
            text(
                """
                INSERT INTO jobs (job_id, org_id, kind, status, files, result)
                VALUES (:job_id, :org_id, :kind, :status, :files, :result)
                """
            ),
            {
                "job_id": job_id,
                "org_id": org_id,
                "kind": kind,
                "status": status,
                "files": files_json,
                "result": result_json,
            },
        )
        return
    connection.execute(
        """
        INSERT INTO jobs (job_id, org_id, kind, status, files, result)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (job_id, org_id, kind, status, files_json, result_json),
    )


def begin_stored_job(
    org_id: str,
    job_kind: str,
    document_kind: str,
    staged: list[dict],
    status: str = QUEUED,
    result: dict | None = None,
) -> str:
    """Insert the file rows and the job in one commit. The bytes are already stored."""
    from core.stored_files import ensure_document_table, insert_document_on

    ensure_jobs_table()
    ensure_document_table()
    job_id = str(uuid.uuid4())
    payload = json.dumps(staged)
    stored_result = json.dumps(result) if result is not None else None

    def write(connection, postgres: bool) -> None:
        for item in staged:
            if item.get("object_key"):
                insert_document_on(connection, postgres, org_id, document_kind, item)
        insert_job_on(
            connection, postgres, job_id, org_id, job_kind, status, payload, stored_result
        )

    _in_transaction(write)
    return job_id


def apply_delete(job: dict) -> dict:
    """Erase the files and rows named on a delete event. The event row stays."""
    item = (job.get("files") or [{}])[0]
    keys = [key for key in item.get("object_keys") or [] if key]
    hashes = [digest for digest in item.get("file_hashes") or [] if digest]
    ids = [job_id for job_id in item.get("job_ids") or [] if job_id and job_id != job.get("job_id")]
    from core.cache import get_cache
    from core.stored_files import discard_object_bytes

    document_ids = discard_object_bytes(job["org_id"], keys)
    cache = get_cache()
    for digest in hashes:
        cache.drop_report(job["org_id"], digest)
    _erase_rows(job["org_id"], document_ids, ids)
    return {"deleted_jobs": ids, "deleted_objects": keys}


def _erase_rows(org_id: str, document_ids: list[str], job_ids: list[str]) -> None:
    """Remove the document rows and the hidden job rows in one commit."""
    if not document_ids and not job_ids:
        return
    ensure_jobs_table()
    from core.stored_files import ensure_document_table

    ensure_document_table()

    def write(connection, postgres: bool) -> None:
        if postgres:
            from sqlalchemy import text

            for document_id in document_ids:
                connection.execute(
                    text("DELETE FROM stored_documents WHERE document_id = :document_id"),
                    {"document_id": document_id},
                )
            for job_id in job_ids:
                connection.execute(
                    text("DELETE FROM jobs WHERE job_id = :job_id AND org_id = :org_id"),
                    {"job_id": job_id, "org_id": org_id},
                )
            return
        for document_id in document_ids:
            connection.execute(
                "DELETE FROM stored_documents WHERE document_id = ?",
                (document_id,),
            )
        for job_id in job_ids:
            connection.execute(
                "DELETE FROM jobs WHERE job_id = ? AND org_id = ?",
                (job_id, org_id),
            )

    _in_transaction(write)
