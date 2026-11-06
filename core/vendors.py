"""A vendor slot belongs to one company.

The person sees the vendor name. The job id stays on the slot.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from core.jobs import _Engine, _sqlite_path, _uses_postgres, get_job


def ensure_vendors_table() -> None:
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(872365)"))
            connection.execute(text(
                """
                CREATE TABLE IF NOT EXISTS vendors (
                    vendor_id TEXT PRIMARY KEY,
                    org_id    TEXT NOT NULL,
                    name      TEXT NOT NULL,
                    job_id    TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_vendors_org ON vendors (org_id, created_at)"
            ))
            connection.execute(text(
                "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS input_mode TEXT"
            ))
            connection.execute(text(
                "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS pending_job_id TEXT"
            ))
            connection.execute(text(
                "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ"
            ))
            connection.execute(text(
                "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS mode_claimed_at TIMESTAMPTZ"
            ))
        return

    connection = sqlite3.connect(_sqlite_path())
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS vendors (
                vendor_id  TEXT PRIMARY KEY,
                org_id     TEXT NOT NULL,
                name       TEXT NOT NULL,
                job_id     TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_vendors_org ON vendors (org_id, created_at)"
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(vendors)")
        }
        if "input_mode" not in columns:
            connection.execute("ALTER TABLE vendors ADD COLUMN input_mode TEXT")
        if "pending_job_id" not in columns:
            connection.execute("ALTER TABLE vendors ADD COLUMN pending_job_id TEXT")
        if "deleted_at" not in columns:
            connection.execute("ALTER TABLE vendors ADD COLUMN deleted_at TEXT")
        if "mode_claimed_at" not in columns:
            connection.execute("ALTER TABLE vendors ADD COLUMN mode_claimed_at TEXT")
        connection.commit()
    finally:
        connection.close()


def create_vendor(org_id: str, name: str) -> dict:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Vendor name is required")
    ensure_vendors_table()
    vendor_id = str(uuid.uuid4())
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO vendors (vendor_id, org_id, name)
                    VALUES (:vendor_id, :org_id, :name)
                    """
                ),
                {"vendor_id": vendor_id, "org_id": org_id, "name": cleaned},
            )
    else:
        connection = sqlite3.connect(_sqlite_path())
        try:
            connection.execute(
                "INSERT INTO vendors (vendor_id, org_id, name) VALUES (?, ?, ?)",
                (vendor_id, org_id, cleaned),
            )
            connection.commit()
        finally:
            connection.close()
    return {"vendor_id": vendor_id, "name": cleaned, "job_id": None}


def get_vendor(org_id: str, vendor_id: str, _repair: bool = True) -> dict | None:
    ensure_vendors_table()
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT vendor_id, org_id, name, job_id, input_mode, pending_job_id, created_at
                    FROM vendors
                    WHERE vendor_id = :vendor_id AND org_id = :org_id
                      AND deleted_at IS NULL
                    """
                ),
                {"vendor_id": vendor_id, "org_id": org_id},
            ).mappings().first()
        found = dict(row) if row else None
        if _repair and found and _repair_succeeded_pending(org_id, found.get("pending_job_id") or ""):
            return get_vendor(org_id, vendor_id, _repair=False)
        return found

    connection = sqlite3.connect(_sqlite_path())
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT vendor_id, org_id, name, job_id, input_mode, pending_job_id, created_at
            FROM vendors
            WHERE vendor_id = ? AND org_id = ?
              AND deleted_at IS NULL
            """,
            (vendor_id, org_id),
        ).fetchone()
    finally:
        connection.close()
    found = dict(row) if row else None
    if _repair and found and _repair_succeeded_pending(org_id, found.get("pending_job_id") or ""):
        return get_vendor(org_id, vendor_id, _repair=False)
    return found


def _repair_succeeded_pending(org_id: str, pending_id: str) -> bool:
    """Finish a replacement whose report was saved before the vendor moved."""
    if not pending_id:
        return False
    pending = get_job(org_id, pending_id)
    if pending is None or pending.get("status") != "succeeded":
        return False
    commit_pending_job(pending_id)
    return True


class SlotTaken(ValueError):
    """Another request already stored a check in this vendor slot."""


def claim_vendor_mode(org_id: str, vendor_id: str, mode: str) -> bool:
    """Set the mode only while it is empty. A stale empty claim can be taken again."""
    ensure_vendors_table()
    from core.jobs import _lease_seconds

    lease = _lease_seconds()
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE vendors
                    SET input_mode = :mode, mode_claimed_at = now()
                    WHERE vendor_id = :vendor_id AND org_id = :org_id
                      AND (
                        input_mode IS NULL OR input_mode = ''
                        OR (
                          (job_id IS NULL OR job_id = '')
                          AND (pending_job_id IS NULL OR pending_job_id = '')
                          AND mode_claimed_at IS NOT NULL
                          AND mode_claimed_at < now() - make_interval(secs => :lease)
                        )
                      )
                    """
                ),
                {"mode": mode, "vendor_id": vendor_id, "org_id": org_id, "lease": lease},
            )
            return result.rowcount == 1
    connection = sqlite3.connect(_sqlite_path())
    try:
        cursor = connection.execute(
            """
            UPDATE vendors
            SET input_mode = ?, mode_claimed_at = datetime('now')
            WHERE vendor_id = ? AND org_id = ?
              AND (
                input_mode IS NULL OR input_mode = ''
                OR (
                  (job_id IS NULL OR job_id = '')
                  AND (pending_job_id IS NULL OR pending_job_id = '')
                  AND mode_claimed_at IS NOT NULL
                  AND mode_claimed_at < datetime('now', ?)
                )
              )
            """,
            (mode, vendor_id, org_id, f"-{lease} seconds"),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def clear_vendor_mode(org_id: str, vendor_id: str) -> None:
    """A first clause failed before a report existed. The slot can start again."""
    ensure_vendors_table()
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE vendors SET input_mode = NULL, job_id = NULL, pending_job_id = NULL
                    WHERE vendor_id = :vendor_id AND org_id = :org_id
                    """
                ),
                {"vendor_id": vendor_id, "org_id": org_id},
            )
        return
    connection = sqlite3.connect(_sqlite_path())
    try:
        connection.execute(
            """
            UPDATE vendors SET input_mode = NULL, job_id = NULL, pending_job_id = NULL
            WHERE vendor_id = ? AND org_id = ?
            """,
            (vendor_id, org_id),
        )
        connection.commit()
    finally:
        connection.close()


def commit_pending_job(job_id: str) -> None:
    """Move the vendor to a succeeded replacement and delete the previous report."""
    ensure_vendors_table()
    from core.jobs import _in_transaction

    def write(connection, postgres: bool) -> None:
        settle_pending_on(connection, postgres, job_id)

    _in_transaction(write)


def settle_pending_on(connection, postgres: bool, job_id: str) -> None:
    """Adopt a finished replacement on the caller's open transaction."""
    row = _pending_vendor_on(connection, postgres, job_id)
    if row is None:
        return
    from core.jobs import CLAUSE_SUBMITTED, REPORT_DELETED, queue_delete

    kind = _job_kind_on(connection, postgres, row["org_id"], job_id)
    mode = "clause" if kind == CLAUSE_SUBMITTED else "contract"
    old_job = row.get("job_id") or ""
    if old_job and old_job != job_id:
        queue_delete(
            row["org_id"],
            REPORT_DELETED,
            row["vendor_id"],
            [old_job],
            vendor_change="adopt",
            adopt_job_id=job_id,
            adopt_mode=mode,
            connection=connection,
            postgres=postgres,
        )
        return
    from core.jobs import _vendor_change

    changed = _vendor_change(
        connection,
        "adopt",
        row["org_id"],
        row["vendor_id"],
        postgres=postgres,
        adopt_job_id=job_id,
        adopt_mode=mode,
    )
    if changed == 0:
        raise LookupError(job_id)


def _pending_vendor_on(connection, postgres: bool, job_id: str) -> dict | None:
    if postgres:
        from sqlalchemy import text

        row = connection.execute(
            text(
                """
                SELECT vendor_id, org_id, job_id
                FROM vendors
                WHERE pending_job_id = :job_id
                FOR UPDATE
                """
            ),
            {"job_id": job_id},
        ).mappings().first()
        return dict(row) if row else None
    row = connection.execute(
        "SELECT vendor_id, org_id, job_id FROM vendors WHERE pending_job_id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return {"vendor_id": row["vendor_id"], "org_id": row["org_id"], "job_id": row["job_id"]}


def _job_kind_on(connection, postgres: bool, org_id: str, job_id: str) -> str:
    if postgres:
        from sqlalchemy import text

        row = connection.execute(
            text("SELECT kind FROM jobs WHERE job_id = :job_id AND org_id = :org_id"),
            {"job_id": job_id, "org_id": org_id},
        ).first()
        return row[0] if row else ""
    row = connection.execute(
        "SELECT kind FROM jobs WHERE job_id = ? AND org_id = ?",
        (job_id, org_id),
    ).fetchone()
    if row is None:
        return ""
    return row["kind"] if hasattr(row, "keys") else row[0]


def hide_vendor(org_id: str, vendor_id: str) -> bool:
    """Remove the vendor now. A vendor.deleted job erases its files.

    When the vendor has a check, the job hide, the delete event, and this
    row commit together. A crash before the commit leaves the vendor and its
    jobs unchanged.
    """
    vendor = get_vendor(org_id, vendor_id)
    if vendor is None:
        return False
    from core.jobs import VENDOR_DELETED, queue_delete

    job_ids = [vendor.get("job_id") or "", vendor.get("pending_job_id") or ""]
    if any(job_ids):
        ensure_vendors_table()
        try:
            queue_delete(
                org_id, VENDOR_DELETED, vendor_id, job_ids, vendor_change="remove"
            )
        except LookupError:
            return False
        return True
    ensure_vendors_table()
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    DELETE FROM vendors
                    WHERE vendor_id = :vendor_id AND org_id = :org_id
                    """
                ),
                {"vendor_id": vendor_id, "org_id": org_id},
            )
        return result.rowcount > 0
    connection = sqlite3.connect(_sqlite_path())
    try:
        cursor = connection.execute(
            "DELETE FROM vendors WHERE vendor_id = ? AND org_id = ?",
            (vendor_id, org_id),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def clear_vendor_report(org_id: str, vendor_id: str) -> str:
    """Detach the report now. A report.deleted job erases its file.

    The job hide, the delete event, and the cleared report link commit
    together. A crash before the commit leaves the report in place.
    """
    vendor = get_vendor(org_id, vendor_id)
    if vendor is None:
        raise ValueError("Vendor not found")
    pending_id = vendor.get("pending_job_id") or ""
    if pending_id:
        pending = get_job(org_id, pending_id)
        if pending and pending.get("status") in ("queued", "running"):
            raise ValueError("A check is still running")
    job_id = vendor.get("job_id") or ""
    if not job_id:
        raise ValueError("No report is stored")
    from core.jobs import REPORT_DELETED, queue_delete

    ensure_vendors_table()
    try:
        queue_delete(org_id, REPORT_DELETED, vendor_id, [job_id], vendor_change="clear")
    except LookupError:
        raise ValueError("Vendor not found") from None
    return job_id


def begin_vendor_contract(org_id: str, vendor_id: str, staged: dict, *, replacing: bool) -> str:
    """Store the contract row, the job, and the vendor link in one commit."""
    ensure_vendors_table()
    from core.jobs import CONTRACT_SUBMITTED, QUEUED, _in_transaction, ensure_jobs_table, insert_job_on
    from core.stored_files import ensure_document_table, insert_document_on

    ensure_jobs_table()
    ensure_document_table()
    job_id = str(uuid.uuid4())
    payload = json.dumps([staged])

    def write(connection, postgres: bool) -> None:
        insert_document_on(connection, postgres, org_id, "contracts", staged)
        insert_job_on(
            connection, postgres, job_id, org_id, CONTRACT_SUBMITTED, QUEUED, payload, None
        )
        changed = _slot_update(connection, postgres, org_id, vendor_id, job_id, replacing)
        if changed != 1:
            raise SlotTaken(
                "A replacement is already running"
                if replacing
                else "This vendor already has a submission"
            )

    _in_transaction(write)
    return job_id


def store_clause_result(
    org_id: str,
    vendor_id: str,
    heading: str,
    report: dict,
    *,
    replacing: bool,
    old_job: str,
) -> str:
    """Store a finished clause and point the vendor at it in one commit."""
    ensure_vendors_table()
    from core.jobs import (
        CLAUSE_SUBMITTED,
        REPORT_DELETED,
        SUCCEEDED,
        _in_transaction,
        ensure_jobs_table,
        insert_job_on,
        queue_delete,
    )

    ensure_jobs_table()
    job_id = str(uuid.uuid4())
    payload = json.dumps([{"filename": heading, "object_key": ""}])
    result = json.dumps(report)

    def write(connection, postgres: bool) -> None:
        insert_job_on(
            connection,
            postgres,
            job_id,
            org_id,
            CLAUSE_SUBMITTED,
            SUCCEEDED,
            payload,
            result,
        )
        if replacing and old_job:
            queue_delete(
                org_id,
                REPORT_DELETED,
                vendor_id,
                [old_job],
                vendor_change="replace",
                adopt_job_id=job_id,
                adopt_mode="clause",
                expected_job_id=old_job,
                connection=connection,
                postgres=postgres,
            )
            return
        if _attach_clause(connection, postgres, org_id, vendor_id, job_id) != 1:
            raise SlotTaken("This vendor already has a submission")

    try:
        _in_transaction(write)
    except LookupError as exc:
        raise SlotTaken("This vendor already has a submission") from exc
    return job_id


def _slot_update(connection, postgres: bool, org_id: str, vendor_id: str, job_id: str, replacing: bool) -> int:
    from core.jobs import _lease_seconds

    lease = _lease_seconds()
    if postgres:
        from sqlalchemy import text

        if replacing:
            statement = """
                UPDATE vendors SET pending_job_id = :job_id
                WHERE vendor_id = :vendor_id AND org_id = :org_id
                  AND (pending_job_id IS NULL OR pending_job_id = '')
                """
        else:
            statement = """
                UPDATE vendors
                SET job_id = :job_id, input_mode = 'contract', mode_claimed_at = NULL
                WHERE vendor_id = :vendor_id AND org_id = :org_id
                  AND (job_id IS NULL OR job_id = '')
                  AND (pending_job_id IS NULL OR pending_job_id = '')
                  AND (
                    input_mode IS NULL OR input_mode = ''
                    OR (
                      mode_claimed_at IS NOT NULL
                      AND mode_claimed_at < now() - make_interval(secs => :lease)
                    )
                  )
                """
        params = {"job_id": job_id, "vendor_id": vendor_id, "org_id": org_id}
        if not replacing:
            params["lease"] = lease
        return connection.execute(text(statement), params).rowcount
    if replacing:
        return connection.execute(
            """
            UPDATE vendors SET pending_job_id = ?
            WHERE vendor_id = ? AND org_id = ?
              AND (pending_job_id IS NULL OR pending_job_id = '')
            """,
            (job_id, vendor_id, org_id),
        ).rowcount
    return connection.execute(
        """
        UPDATE vendors
        SET job_id = ?, input_mode = 'contract', mode_claimed_at = NULL
        WHERE vendor_id = ? AND org_id = ?
          AND (job_id IS NULL OR job_id = '')
          AND (pending_job_id IS NULL OR pending_job_id = '')
          AND (
            input_mode IS NULL OR input_mode = ''
            OR (
              mode_claimed_at IS NOT NULL
              AND mode_claimed_at < datetime('now', ?)
            )
          )
        """,
        (job_id, vendor_id, org_id, f"-{lease} seconds"),
    ).rowcount


def _attach_clause(connection, postgres: bool, org_id: str, vendor_id: str, job_id: str) -> int:
    if postgres:
        from sqlalchemy import text

        return connection.execute(
            text(
                """
                UPDATE vendors
                SET job_id = :job_id, mode_claimed_at = NULL
                WHERE vendor_id = :vendor_id AND org_id = :org_id
                  AND input_mode = 'clause'
                  AND (job_id IS NULL OR job_id = '')
                  AND (pending_job_id IS NULL OR pending_job_id = '')
                """
            ),
            {"job_id": job_id, "vendor_id": vendor_id, "org_id": org_id},
        ).rowcount
    return connection.execute(
        """
        UPDATE vendors
        SET job_id = ?, mode_claimed_at = NULL
        WHERE vendor_id = ? AND org_id = ?
          AND input_mode = 'clause'
          AND (job_id IS NULL OR job_id = '')
          AND (pending_job_id IS NULL OR pending_job_id = '')
        """,
        (job_id, vendor_id, org_id),
    ).rowcount


def list_vendors(org_id: str, _repair: bool = True) -> list[dict]:
    """Newest vendor first. The current contract, if any, is the job on the slot."""
    ensure_vendors_table()
    if _uses_postgres():
        from sqlalchemy import text

        with _Engine() as engine, engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT vendor_id, name, job_id, input_mode, pending_job_id, created_at
                    FROM vendors
                    WHERE org_id = :org_id AND deleted_at IS NULL
                    ORDER BY created_at DESC
                    """
                ),
                {"org_id": org_id},
            ).mappings().all()
        stored = [dict(row) for row in rows]
    else:
        connection = sqlite3.connect(_sqlite_path())
        connection.row_factory = sqlite3.Row
        try:
            stored = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT vendor_id, name, job_id, input_mode, pending_job_id, created_at
                    FROM vendors
                    WHERE org_id = ? AND deleted_at IS NULL
                    ORDER BY created_at DESC
                    """,
                    (org_id,),
                ).fetchall()
            ]
        finally:
            connection.close()
    if _repair and any(
        _repair_succeeded_pending(org_id, row.get("pending_job_id") or "") for row in stored
    ):
        return list_vendors(org_id, _repair=False)
    return [_public_vendor(org_id, row) for row in stored]


def _public_vendor(org_id: str, row: dict) -> dict:
    created = row["created_at"]
    shown = {
        "vendor_id": row["vendor_id"],
        "name": row["name"],
        "job_id": row["job_id"],
        "input_mode": row.get("input_mode") or "",
        "pending_job_id": row.get("pending_job_id") or "",
        "filename": "",
        "status": "",
        "pending_status": "",
        "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created),
        "updated_at": "",
    }
    pending_id = row.get("pending_job_id") or ""
    if pending_id:
        pending = get_job(org_id, pending_id)
        if pending is not None:
            shown["pending_status"] = pending["status"]
    if not row["job_id"]:
        return shown
    job = get_job(org_id, row["job_id"])
    if job is None:
        return shown
    shown["filename"] = job.get("filename") or ""
    shown["status"] = job["status"]
    shown["updated_at"] = job.get("updated_at") or ""
    return shown
