"""One shared username and password for a company.

A new login increments session_version. A token from the previous login
no longer matches. The password is stored as a slow hash, never as text.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from core.tenant import TenantConfig, find_tenant_by_login, save_tenant_config
from core.tenant_repository import get_tenant_repository

_ITERATIONS = 200_000
_TOKEN_SECONDS = 60 * 60


def _secret() -> bytes:
    value = os.getenv("JWT_SECRET")
    if not value:
        raise RuntimeError("JWT_SECRET is not set.")
    return value.encode("utf-8")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(rounds),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def issue_token(org_id: str, session_version: int) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
    payload = _b64(json.dumps({
        "org_id": org_id,
        "sv": int(session_version),
        "exp": int(time.time()) + _TOKEN_SECONDS,
    }).encode("utf-8"))
    signature = hmac.new(_secret(), f"{header}.{payload}".encode("ascii"), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def company_from_token(token: str) -> str:
    try:
        header, payload, signature = token.split(".")
        expected = hmac.new(
            _secret(), f"{header}.{payload}".encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64_decode(signature), expected):
            raise ValueError("Login token is not valid")
        body = json.loads(_b64_decode(payload))
    except (ValueError, json.JSONDecodeError, RuntimeError) as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise ValueError("Login token is not valid") from exc
    if int(body.get("exp", 0)) < int(time.time()):
        raise ValueError("Login expired")
    org_id = str(body.get("org_id") or "")
    company = get_tenant_repository().get(org_id)
    if company is None or int(company.session_version or 0) != int(body.get("sv", -1)):
        raise ValueError("This login was replaced")
    return org_id


def register_company(org_id: str, login_name: str, password: str, name: str = "") -> tuple[str, str]:
    company_id = (org_id or "").strip()
    username = (login_name or "").strip().lower()
    if not company_id or not username or len(password or "") < 8:
        raise ValueError(
            "org_id, login_name, and a password of at least 8 characters are required"
        )
    repository = get_tenant_repository()
    taken = repository.find_by_login(username)
    if taken is not None and taken.org_id != company_id:
        raise ValueError("That username is already used")
    current = repository.get(company_id)
    if current is not None and current.login_name:
        raise ValueError("This company already has a login")
    if current is None:
        current = TenantConfig(org_id=company_id, name=name or company_id)
    elif name:
        current.name = name
    current.login_name = username
    current.password_hash = hash_password(password)
    current.session_version = 1
    try:
        save_tenant_config(current)
    except RuntimeError as exc:
        _raise_if_already_saved(exc)
        raise
    return issue_token(company_id, 1), company_id


def _raise_if_already_saved(exc: BaseException) -> None:
    """Two copies can pass the first check and then hit the unique index."""
    text = str(exc).lower()
    if "unique" not in text and "duplicate" not in text:
        return
    if "org_id" in text:
        raise ValueError("This company already has a login") from exc
    raise ValueError("That username is already used") from exc


def login_company(login_name: str, password: str) -> tuple[str, str]:
    username = (login_name or "").strip().lower()
    company = find_tenant_by_login(username)
    if (
        company is None
        or not company.password_hash
        or not password_matches(password or "", company.password_hash)
    ):
        raise ValueError("Unknown username or password")
    company.session_version = int(company.session_version or 0) + 1
    save_tenant_config(company)
    return issue_token(company.org_id, company.session_version), company.org_id
