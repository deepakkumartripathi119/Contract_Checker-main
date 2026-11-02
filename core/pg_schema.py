"""Postgres schema for policy chunks and tenant configuration.

The compliance cache stays in SQLite. This module does not create cache tables.
"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

EMBEDDING_DIMENSIONS = 1024

SCHEMA_STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """
CREATE TABLE IF NOT EXISTS policy_chunks (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    parent_id   TEXT,
    chunk_type  TEXT NOT NULL DEFAULT 'child',
    text        TEXT NOT NULL,
    breadcrumb  TEXT NOT NULL DEFAULT '',
    metadata    JSONB NOT NULL DEFAULT CAST('{}' AS jsonb),
    embedding   vector(1024),
    UNIQUE (org_id, id)
)
""",
    """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'policy_chunks_parent_fk'
    ) THEN
        ALTER TABLE policy_chunks
            ADD CONSTRAINT policy_chunks_parent_fk
            FOREIGN KEY (org_id, parent_id)
            REFERENCES policy_chunks (org_id, id);
    END IF;
END $$
""",
    "CREATE INDEX IF NOT EXISTS idx_policy_chunks_org ON policy_chunks (org_id)",
    "CREATE INDEX IF NOT EXISTS idx_policy_chunks_doc ON policy_chunks (org_id, doc_id)",
    "CREATE INDEX IF NOT EXISTS idx_policy_chunks_parent ON policy_chunks (parent_id)",
    """
CREATE TABLE IF NOT EXISTS tenant_configs (
    org_id                       TEXT PRIMARY KEY,
    name                         TEXT NOT NULL,
    aliases                      JSONB NOT NULL DEFAULT CAST('[]' AS jsonb),
    active_policy_version        TEXT NOT NULL DEFAULT 'v1.0',
    silent_policy_behavior       TEXT NOT NULL DEFAULT 'review_required',
    favorable_deviation_behavior TEXT NOT NULL DEFAULT 'allow_favorable',
    supported_languages          JSONB NOT NULL DEFAULT CAST('["en"]' AS jsonb),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
)
""",
    "ALTER TABLE tenant_configs ADD COLUMN IF NOT EXISTS login_name TEXT",
    "ALTER TABLE tenant_configs ADD COLUMN IF NOT EXISTS password_hash TEXT",
    "ALTER TABLE tenant_configs ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0",
    """
CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_login_name
ON tenant_configs (login_name)
WHERE login_name IS NOT NULL
""",
]


def apply_schema(connection: Connection) -> None:
    """Create the extension and tables. Safe to run more than once."""
    connection.execute(text("SELECT pg_advisory_xact_lock(872364)"))
    for statement in SCHEMA_STATEMENTS:
        connection.execute(text(statement))
