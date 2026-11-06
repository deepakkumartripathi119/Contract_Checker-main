"""Tenant persistence.

core.tenant owns the tenant rules and the in-memory copy used by a running
check. This module is the only place that knows whether that row lives in
SQLite or Postgres. Callers ask core.tenant for a tenant; they do not import
this module.
"""

from __future__ import annotations

import json
import os
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from core.tenant import TenantConfig


_TENANT_DB_TABLE = """
CREATE TABLE IF NOT EXISTS tenant_configs (
    org_id                       TEXT PRIMARY KEY,
    name                         TEXT NOT NULL,
    aliases                      TEXT NOT NULL DEFAULT '[]',
    active_policy_version        TEXT NOT NULL DEFAULT 'v1.0',
    silent_policy_behavior       TEXT NOT NULL DEFAULT 'review_required',
    favorable_deviation_behavior TEXT NOT NULL DEFAULT 'allow_favorable',
    supported_languages          TEXT NOT NULL DEFAULT '["en"]',
    updated_at                   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_MEM_CONN: Optional[sqlite3.Connection] = None


class TenantRepository(ABC):
    @abstractmethod
    def get(self, org_id: str) -> Optional[TenantConfig]:
        """Return the saved tenant, or None when no row exists."""

    @abstractmethod
    def save(self, config: TenantConfig) -> None:
        """Insert or replace the tenant row."""

    @abstractmethod
    def find_by_login(self, login_name: str) -> Optional[TenantConfig]:
        """Return the company for a shared username, or None."""

    @abstractmethod
    def increment_session(self, org_id: str) -> int:
        """Add one to the login session and return the new number."""


def _json_list(value) -> list:
    if isinstance(value, str):
        return json.loads(value)
    return list(value or [])


def _config_from_mapping(row) -> TenantConfig:
    def field(name, default=None):
        try:
            value = row[name]
        except (KeyError, IndexError):
            return default
        return default if value is None else value

    return TenantConfig(
        org_id=row["org_id"],
        name=row["name"],
        aliases=_json_list(row["aliases"]),
        active_policy_version=row["active_policy_version"],
        silent_policy_behavior=row["silent_policy_behavior"],
        favorable_deviation_behavior=row["favorable_deviation_behavior"],
        supported_languages=_json_list(row["supported_languages"]),
        business_email=field("business_email", "") or "",
        industry=field("industry", "") or "",
        company_size=field("company_size", "") or "",
        headquarters=field("headquarters", "") or "",
        website=field("website", "") or "",
        login_name=field("login_name"),
        password_hash=field("password_hash"),
        session_version=int(field("session_version", 0) or 0),
    )


def _ensure_sqlite_login_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tenant_configs)")}
    if "login_name" not in columns:
        conn.execute("ALTER TABLE tenant_configs ADD COLUMN login_name TEXT")
    if "password_hash" not in columns:
        conn.execute("ALTER TABLE tenant_configs ADD COLUMN password_hash TEXT")
    if "session_version" not in columns:
        conn.execute(
            "ALTER TABLE tenant_configs ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0"
        )
    for column in ("business_email", "industry", "company_size", "headquarters", "website"):
        if column not in columns:
            conn.execute(f"ALTER TABLE tenant_configs ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_login_name
        ON tenant_configs(login_name)
        WHERE login_name IS NOT NULL
        """
    )


class SqliteTenantRepository(TenantRepository):
    def _connect(self) -> sqlite3.Connection:
        global _MEM_CONN
        db_path = os.getenv(
            "SQLITE_DB_PATH",
            str(Path(__file__).resolve().parents[1] / "data" / "contract_checker.db"),
        )
        if db_path == ":memory:":
            if _MEM_CONN is None:
                _MEM_CONN = sqlite3.connect(":memory:", check_same_thread=False)
                _MEM_CONN.row_factory = sqlite3.Row
                with _MEM_CONN:
                    _MEM_CONN.executescript(_TENANT_DB_TABLE)
                    _ensure_sqlite_login_columns(_MEM_CONN)
            else:
                _ensure_sqlite_login_columns(_MEM_CONN)
            return _MEM_CONN

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        with conn:
            conn.executescript(_TENANT_DB_TABLE)
            _ensure_sqlite_login_columns(conn)
        return conn

    def get(self, org_id: str) -> Optional[TenantConfig]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM tenant_configs WHERE org_id = ?", (org_id,)
            ).fetchone()
        finally:
            if conn is not _MEM_CONN:
                conn.close()
        if not row:
            return None
        return _config_from_mapping(row)

    def save(self, config: TenantConfig) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO tenant_configs (
                    org_id, name, aliases, active_policy_version,
                    silent_policy_behavior, favorable_deviation_behavior,
                    supported_languages, login_name, password_hash,
                    session_version, business_email, industry, company_size,
                    headquarters, website, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(org_id) DO UPDATE SET
                    name = excluded.name,
                    aliases = excluded.aliases,
                    active_policy_version = excluded.active_policy_version,
                    silent_policy_behavior = excluded.silent_policy_behavior,
                    favorable_deviation_behavior = excluded.favorable_deviation_behavior,
                    supported_languages = excluded.supported_languages,
                    login_name = excluded.login_name,
                    business_email = excluded.business_email,
                    industry = excluded.industry,
                    company_size = excluded.company_size,
                    headquarters = excluded.headquarters,
                    website = excluded.website,
                    password_hash = excluded.password_hash,
                    session_version = MAX(tenant_configs.session_version, excluded.session_version),
                    updated_at = datetime('now')
                """,
                (
                    config.org_id,
                    config.name,
                    json.dumps(config.aliases),
                    config.active_policy_version,
                    config.silent_policy_behavior,
                    config.favorable_deviation_behavior,
                    json.dumps(config.supported_languages),
                    config.login_name,
                    config.password_hash,
                    int(config.session_version or 0),
                    config.business_email,
                    config.industry,
                    config.company_size,
                    config.headquarters,
                    config.website,
                ),
            )
            conn.commit()
        finally:
            if conn is not _MEM_CONN:
                conn.close()

    def find_by_login(self, login_name: str) -> Optional[TenantConfig]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM tenant_configs WHERE login_name = ?",
                (login_name,),
            ).fetchone()
        finally:
            if conn is not _MEM_CONN:
                conn.close()
        return _config_from_mapping(row) if row else None

    def increment_session(self, org_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                UPDATE tenant_configs
                SET session_version = session_version + 1,
                    updated_at = datetime('now')
                WHERE org_id = ?
                RETURNING session_version
                """,
                (org_id,),
            ).fetchone()
            conn.commit()
        finally:
            if conn is not _MEM_CONN:
                conn.close()
        if row is None:
            raise RuntimeError(f"No company '{org_id}' to sign in.")
        return int(row["session_version"])


class PostgresTenantRepository(TenantRepository):
    def _engine(self):
        from sqlalchemy import create_engine

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not set.")
        return create_engine(database_url)

    def _ensure_login_columns(self, connection) -> None:
        from core.pg_schema import apply_schema

        apply_schema(connection)

    def get(self, org_id: str) -> Optional[TenantConfig]:
        from sqlalchemy import text

        with self._engine().begin() as connection:
            self._ensure_login_columns(connection)
            row = connection.execute(
                text("SELECT * FROM tenant_configs WHERE org_id = :org_id"),
                {"org_id": org_id},
            ).mappings().fetchone()
        if not row:
            return None
        return _config_from_mapping(row)

    def find_by_login(self, login_name: str) -> Optional[TenantConfig]:
        from sqlalchemy import text

        with self._engine().begin() as connection:
            self._ensure_login_columns(connection)
            row = connection.execute(
                text("SELECT * FROM tenant_configs WHERE login_name = :login_name"),
                {"login_name": login_name},
            ).mappings().fetchone()
        if not row:
            return None
        return _config_from_mapping(row)

    def save(self, config: TenantConfig) -> None:
        from sqlalchemy import text

        with self._engine().begin() as connection:
            self._ensure_login_columns(connection)
            connection.execute(
                text(
                    """
                    INSERT INTO tenant_configs (
                        org_id, name, aliases, active_policy_version,
                        silent_policy_behavior, favorable_deviation_behavior,
                        supported_languages, login_name, password_hash,
                        session_version, business_email, industry, company_size,
                        headquarters, website, updated_at
                    ) VALUES (
                        :org_id, :name, CAST(:aliases AS jsonb), :version,
                        :silent, :favorable, CAST(:languages AS jsonb),
                        :login_name, :password_hash, :session_version,
                        :business_email, :industry, :company_size,
                        :headquarters, :website, now()
                    )
                    ON CONFLICT (org_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        aliases = EXCLUDED.aliases,
                        active_policy_version = EXCLUDED.active_policy_version,
                        silent_policy_behavior = EXCLUDED.silent_policy_behavior,
                        favorable_deviation_behavior = EXCLUDED.favorable_deviation_behavior,
                        supported_languages = EXCLUDED.supported_languages,
                        login_name = EXCLUDED.login_name,
                        business_email = EXCLUDED.business_email,
                        industry = EXCLUDED.industry,
                        company_size = EXCLUDED.company_size,
                        headquarters = EXCLUDED.headquarters,
                        website = EXCLUDED.website,
                        password_hash = EXCLUDED.password_hash,
                        session_version = GREATEST(tenant_configs.session_version, EXCLUDED.session_version),
                        updated_at = now()
                    """
                ),
                {
                    "org_id": config.org_id,
                    "name": config.name,
                    "aliases": json.dumps(config.aliases),
                    "version": config.active_policy_version,
                    "silent": config.silent_policy_behavior,
                    "favorable": config.favorable_deviation_behavior,
                    "languages": json.dumps(config.supported_languages),
                    "login_name": config.login_name,
                    "password_hash": config.password_hash,
                    "session_version": int(config.session_version or 0),
                    "business_email": config.business_email,
                    "industry": config.industry,
                    "company_size": config.company_size,
                    "headquarters": config.headquarters,
                    "website": config.website,
                },
            )

    def increment_session(self, org_id: str) -> int:
        from sqlalchemy import text

        engine = self._engine()
        try:
            with engine.begin() as connection:
                self._ensure_login_columns(connection)
                row = connection.execute(
                    text(
                        """
                        UPDATE tenant_configs
                        SET session_version = session_version + 1, updated_at = now()
                        WHERE org_id = :org_id
                        RETURNING session_version
                        """
                    ),
                    {"org_id": org_id},
                ).first()
        finally:
            engine.dispose()
        if row is None:
            raise RuntimeError(f"No company '{org_id}' to sign in.")
        return int(row[0])


def get_tenant_repository() -> TenantRepository:
    """Return the tenant database selected by VECTOR_STORE_BACKEND."""
    if os.getenv("VECTOR_STORE_BACKEND", "sqlite").lower() == "pgvector":
        return PostgresTenantRepository()
    return SqliteTenantRepository()
