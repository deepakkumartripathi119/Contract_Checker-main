"""Process settings.

Feature code reads configuration from this module. A database, model, or
cache change does not belong in here, and this module does not import those
features.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def get_config(key: str, default: str | None = None) -> str | None:
    """Read a configuration value from environment variables only."""
    return os.getenv(key, default)


def get_default_org_id() -> str:
    """Return the configured organization, or a neutral local default."""
    return get_config("POLICY_ORG_ID", "default") or "default"
