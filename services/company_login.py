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

from core.tenant import TenantConfig, advance_login_session, find_tenant_by_login, save_tenant_config
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


_COMPANY_SIZES = {"1–10", "11–50", "51–200", "201–500", "501–1,000", "1,000+"}


def _company_id_from_name(name: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug[:48]


def _business_email(value: str) -> str:
    text = (value or "").strip().lower()
    if text.count("@") != 1:
        raise ValueError("Enter a business email")
    local, domain = text.split("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("Enter a business email")
    return text


def register_company(
    org_id: str,
    login_name: str,
    password: str,
    name: str = "",
    business_email: str = "",
    industry: str = "",
    company_size: str = "",
    headquarters: str = "",
    website: str = "",
) -> tuple[str, str]:
    email = _business_email(business_email) if (business_email or "").strip() else ""
    username = (login_name or email).strip().lower()
    company_id = (org_id or _company_id_from_name(name)).strip()
    if not company_id or not username or len(password or "") < 8:
        raise ValueError(
            "A company name, a business email, and a password of at least 8 characters are required"
        )
    size = (company_size or "").strip()
    if size and size not in _COMPANY_SIZES:
        raise ValueError("Choose a company size")
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
    current.business_email = email or current.business_email
    current.industry = (industry or "").strip()
    current.company_size = size
    current.headquarters = (headquarters or "").strip()
    current.website = (website or "").strip()
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


def company_profile(org_id: str) -> dict:
    """The stored company row. The password hash is not included."""
    company = get_tenant_repository().get(org_id)
    if company is None:
        raise ValueError("Unknown company")
    return {
        "org_id": company.org_id,
        "name": company.name or "",
        "business_email": company.business_email or company.login_name or "",
        "industry": company.industry or "",
        "company_size": company.company_size or "",
        "headquarters": company.headquarters or "",
        "website": company.website or "",
    }


def change_company_account(
    org_id: str,
    current_password: str,
    business_email: str = "",
    new_password: str = "",
) -> dict:
    """Change the sign-in email, the password, or both. Registration fields stay."""
    company = get_tenant_repository().get(org_id)
    if (
        company is None
        or not company.password_hash
        or not password_matches(current_password or "", company.password_hash)
    ):
        raise ValueError("Current password is wrong")
    email = (business_email or "").strip()
    password = new_password or ""
    current_login = (company.login_name or "").strip().lower()
    if email and email.lower() != current_login:
        new_email = _business_email(email)
        taken = get_tenant_repository().find_by_login(new_email)
        if taken is not None and taken.org_id != org_id:
            raise ValueError("That username is already used")
        company.business_email = new_email
        company.login_name = new_email
    if password:
        if len(password) < 8:
            raise ValueError("A password of at least 8 characters is required")
        company.password_hash = hash_password(password)
    if not email and not password:
        raise ValueError("Enter a new email or a new password")
    save_tenant_config(company)
    result = {"name": company.name or company.org_id}
    if password:
        result["token"] = issue_token(org_id, advance_login_session(org_id))
    return result


def login_company(login_name: str, password: str) -> tuple[str, str]:
    username = (login_name or "").strip().lower()
    company = find_tenant_by_login(username)
    if (
        company is None
        or not company.password_hash
        or not password_matches(password or "", company.password_hash)
    ):
        raise ValueError("Unknown username or password")
    version = advance_login_session(company.org_id)
    return issue_token(company.org_id, version), company.org_id
