"""Create the local file folder and the database tables before the server accepts traffic."""

from __future__ import annotations

import os
from pathlib import Path


def prepare() -> None:
    backend = os.getenv("OBJECT_STORE_BACKEND", "local").lower()
    if backend == "local":
        root = os.getenv("OBJECT_STORE_PATH") or str(
            Path(__file__).resolve().parents[1] / "data" / "objects"
        )
        Path(root).mkdir(parents=True, exist_ok=True)

    if os.getenv("VECTOR_STORE_BACKEND", "sqlite").lower() == "pgvector":
        from sqlalchemy import create_engine

        from core.jobs import ensure_jobs_table
        from core.pg_schema import apply_schema
        from core.stored_files import ensure_document_table

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not set.")
        engine = create_engine(database_url)
        with engine.begin() as connection:
            apply_schema(connection)
        ensure_document_table()
        ensure_jobs_table()
        return

    from core.jobs import ensure_jobs_table
    from core.stored_files import ensure_document_table

    ensure_document_table()
    ensure_jobs_table()


if __name__ == "__main__":
    prepare()
