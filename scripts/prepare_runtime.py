"""Create the local file folder and the database tables before the server accepts traffic."""

from __future__ import annotations

import os
from pathlib import Path


def prepare() -> None:
    _require_remote_settings()
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
        try:
            with engine.begin() as connection:
                apply_schema(connection)
            ensure_document_table()
            ensure_jobs_table()
        finally:
            engine.dispose()
        return

    from core.jobs import ensure_jobs_table
    from core.stored_files import ensure_document_table

    ensure_document_table()
    ensure_jobs_table()


def _require_remote_settings() -> None:
    """Fail before traffic when a managed Redis or bucket is selected but not named."""
    if os.getenv("CACHE_BACKEND", "sqlite").lower() == "redis":
        url = (os.getenv("REDIS_URL") or "").strip()
        if not url:
            raise RuntimeError("REDIS_URL is not set.")
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        try:
            client.ping()
        except Exception as exc:
            raise RuntimeError("Redis did not answer.") from exc
        finally:
            client.close()
    if os.getenv("OBJECT_STORE_BACKEND", "local").lower() == "s3":
        if not (os.getenv("S3_BUCKET") or "").strip():
            raise RuntimeError("S3_BUCKET is not set.")


if __name__ == "__main__":
    prepare()
