"""
core/tenant.py — Tenant configuration and identity.

This module owns the tenant rules: who the organization is, which policy
revision is active, and how silence is treated. It does not know the SQL
for SQLite or Postgres. Those live in core.tenant_repository.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

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
        default_factory=lambda: os.getenv("DEFAULT_SILENT_POLICY_BEHAVIOR", "review_required"),
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
    login_name: Optional[str] = Field(
        default=None,
        description="Shared username for this company. One login for every employee.",
    )
    password_hash: Optional[str] = Field(default=None)
    session_version: int = Field(
        default=0,
        description="Incremented on each login so the previous token stops working.",
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


_IN_MEMORY_TENANTS: dict[str, TenantConfig] = {}


def _repository():
    from core.tenant_repository import get_tenant_repository
    return get_tenant_repository()


def get_tenant_config(org_id: str) -> TenantConfig:
    """Retrieve tenant configuration by org_id, falling back to a neutral default."""
    if not org_id:
        org_id = os.getenv("POLICY_ORG_ID", "default")

    if org_id in _IN_MEMORY_TENANTS:
        return _IN_MEMORY_TENANTS[org_id]

    try:
        config = _repository().get(org_id)
        if config:
            _IN_MEMORY_TENANTS[org_id] = config
            return config
    except Exception as exc:
        print(f"[Warning] Error reading tenant configuration for '{org_id}': {exc}")

    # Missing or unreadable configuration is not evidence of compliance.
    # Do not persist this fallback over a saved tenant preference.
    default_cfg = TenantConfig(
        org_id=org_id,
        name=org_id.capitalize() if org_id not in ("default", "") else "Organization",
        aliases=[],
        active_policy_version="v1.0",
        silent_policy_behavior=os.getenv("DEFAULT_SILENT_POLICY_BEHAVIOR", "review_required"),
    )
    _IN_MEMORY_TENANTS[org_id] = default_cfg
    return default_cfg


def find_tenant_by_login(login_name: str) -> Optional[TenantConfig]:
    """Read the company for a username from the database, not the memory copy."""
    return _repository().find_by_login(login_name)


def save_tenant_config(config: TenantConfig) -> None:
    """Save or update tenant configuration. Fails visibly if persistence fails."""
    try:
        _repository().save(config)
    except Exception as e:
        raise RuntimeError(
            f"Failed to persist tenant configuration for '{config.org_id}': {e}"
        ) from e
    _IN_MEMORY_TENANTS[config.org_id] = config


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
