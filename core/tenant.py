"""
core/tenant.py — Tenant configuration, identity, and isolation abstraction.

Provides explicit tenant configuration boundaries:
- Stable tenant identifier and legal entity names/aliases
- Active policy revision tracking for safe cache invalidation
- Authorized evaluation rules (e.g., treatment of policy silence: review_required vs compliant)
- Cross-tenant isolation guarantees
"""

from __future__ import annotations

import os
import re
import json
import sqlite3
from pathlib import Path
from typing import Optional, List
from pydantic import BaseModel, Field


class TenantConfig(BaseModel):
    """Configuration and legal identity boundary for an organization."""
    org_id: str = Field(..., description="Stable, unique organization identifier")
    name: str = Field(default="", description="Primary legal entity name of the organization")
    aliases: List[str] = Field(
        default_factory=list,
        description="Known aliases, subsidiaries, or trade names identifying the organization in contracts",
    )
    active_policy_version: str = Field(
        default="v1.0",
        description="Active policy revision identifier, incremented on staged policy updates",
    )
    silent_policy_behavior: str = Field(
        default_factory=lambda: os.getenv("DEFAULT_SILENT_POLICY_BEHAVIOR", "compliant"),
        description="Behavior when no internal policy addresses a contract topic: 'review_required' or 'compliant'",
    )
    favorable_deviation_behavior: str = Field(
        default="allow_favorable",
        description="Treatment of vendor deviations that strictly favor the tenant: 'allow_favorable' or 'strict'",
    )
    supported_languages: List[str] = Field(
        default_factory=lambda: ["en"],
        description="List of verified supported languages for policy and contract processing",
    )

    def is_tenant_name(self, candidate: str) -> bool:
        """Check whether candidate string refers to this organization."""
        candidate_norm = candidate.strip().lower()
        if not candidate_norm:
            return False
        if self.name and self.name.strip().lower() == candidate_norm:
            return True
        if self.org_id.strip().lower() == candidate_norm:
            return True
        for alias in self.aliases:
            if alias.strip().lower() == candidate_norm:
                return True
        return False


_TENANT_DB_TABLE = """
CREATE TABLE IF NOT EXISTS tenant_configs (
    org_id                       TEXT PRIMARY KEY,
    name                         TEXT NOT NULL,
    aliases                      TEXT NOT NULL DEFAULT '[]',
    active_policy_version        TEXT NOT NULL DEFAULT 'v1.0',
    silent_policy_behavior       TEXT NOT NULL DEFAULT 'compliant',
    favorable_deviation_behavior TEXT NOT NULL DEFAULT 'allow_favorable',
    supported_languages          TEXT NOT NULL DEFAULT '["en"]',
    updated_at                   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_IN_MEMORY_TENANTS: dict[str, TenantConfig] = {}
_MEM_CONN: Optional[sqlite3.Connection] = None


def _get_tenant_db_conn() -> sqlite3.Connection:
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
        return _MEM_CONN

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.executescript(_TENANT_DB_TABLE)
    return conn


def get_tenant_config(org_id: str) -> TenantConfig:
    """Retrieve tenant configuration by org_id, falling back to neutral default."""
    if not org_id:
        org_id = os.getenv("POLICY_ORG_ID", "default")

    if org_id in _IN_MEMORY_TENANTS:
        return _IN_MEMORY_TENANTS[org_id]

    try:
        conn = _get_tenant_db_conn()
        with conn:
            row = conn.execute(
                "SELECT * FROM tenant_configs WHERE org_id = ?", (org_id,)
            ).fetchone()
        if row:
            config = TenantConfig(
                org_id=row["org_id"],
                name=row["name"],
                aliases=json.loads(row["aliases"]),
                active_policy_version=row["active_policy_version"],
                silent_policy_behavior=row["silent_policy_behavior"],
                favorable_deviation_behavior=row["favorable_deviation_behavior"],
                supported_languages=json.loads(row["supported_languages"]),
            )
            _IN_MEMORY_TENANTS[org_id] = config
            return config
    except Exception as exc:
        print(f"[Warning] Error reading tenant configuration for '{org_id}': {exc}")

    # Default tenant config
    default_cfg = TenantConfig(
        org_id=org_id,
        name=org_id.capitalize() if org_id not in ("default", "") else "Organization",
        aliases=[],
        active_policy_version="v1.0",
        silent_policy_behavior=os.getenv("DEFAULT_SILENT_POLICY_BEHAVIOR", "compliant"),
    )
    _IN_MEMORY_TENANTS[org_id] = default_cfg
    return default_cfg


def save_tenant_config(config: TenantConfig) -> None:
    """Save or update tenant configuration. Fails visibly if persistence fails."""
    try:
        conn = _get_tenant_db_conn()
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tenant_configs (
                    org_id, name, aliases, active_policy_version,
                    silent_policy_behavior, favorable_deviation_behavior,
                    supported_languages, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    config.org_id,
                    config.name,
                    json.dumps(config.aliases),
                    config.active_policy_version,
                    config.silent_policy_behavior,
                    config.favorable_deviation_behavior,
                    json.dumps(config.supported_languages),
                ),
            )
        _IN_MEMORY_TENANTS[config.org_id] = config
    except Exception as e:
        raise RuntimeError(
            f"Failed to persist tenant configuration for '{config.org_id}': {e}"
        ) from e


def bump_tenant_policy_version(org_id: str) -> str:
    """Increment policy revision identifier for a tenant (e.g. v1.0 -> v1.1)."""
    cfg = get_tenant_config(org_id)
    current_ver = cfg.active_policy_version or "v1.0"
    match = re.search(r"v?(\d+)(?:\.(\d+))?", current_ver)
    if match:
        major = int(match.group(1))
        minor = int(match.group(2) or "0") + 1
        new_ver = f"v{major}.{minor}"
    else:
        new_ver = f"{current_ver}.1"
    cfg.active_policy_version = new_ver
    save_tenant_config(cfg)
    return new_ver
