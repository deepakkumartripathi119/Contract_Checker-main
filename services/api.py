"""HTTP door for the checker.

org_id is not accepted from the request body. It comes from the company login token.
"""

from __future__ import annotations

import socket
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from services.company_login import company_from_token, login_company, register_company
from core.jobs import get_job
from services.contract_check import check_clause, submit_contract, submit_policies

_DASHBOARD = Path(__file__).resolve().parents[1] / "apps" / "dashboard" / "index.html"


app = FastAPI(
    title="Contract Checker",
    version="0.1.0",
    description=(
        "Accepts a policy or a contract, stores the file, and returns a job id. "
        "The worker runs the checker. The organization id comes from the login token."
    ),
)


class ClauseCheck(BaseModel):
    clause_text: str
    clause_type: str


class RegisterCompany(BaseModel):
    org_id: str
    login_name: str
    password: str = Field(min_length=8)
    name: str = ""


class LoginCompany(BaseModel):
    login_name: str
    password: str


def require_company(authorization: str | None = Header(default=None)) -> str:
    """Company id comes from the login token, not from the request body."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Login token is required")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return company_from_token(token)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _pdf_name(upload: UploadFile) -> str:
    name = upload.filename or "document.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")
    return name


@app.middleware("http")
async def name_the_copy(request, call_next):
    """Name which API copy answered. The copies do not keep their own sessions."""
    response = await call_next(request)
    response.headers["X-Served-By"] = socket.gethostname()
    return response


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(_DASHBOARD)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/register")
def register(body: RegisterCompany) -> dict:
    try:
        token, company_id = register_company(body.org_id, body.login_name, body.password, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"token": token, "token_type": "bearer", "org_id": company_id}


@app.post("/v1/login")
def login(body: LoginCompany) -> dict:
    try:
        token, company_id = login_company(body.login_name, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"token": token, "token_type": "bearer", "org_id": company_id}


@app.post("/v1/policies")
async def upload_policies(
    org_id: str = Depends(require_company),
    files: list[UploadFile] = File(...),
) -> dict:
    payload: list[tuple[str, bytes]] = []
    for upload in files:
        payload.append((_pdf_name(upload), await upload.read()))
    try:
        stored = submit_policies(payload, org_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"org_id": org_id.strip(), **stored}


@app.post("/v1/checks")
async def check_contract_pdf(
    org_id: str = Depends(require_company),
    contract: UploadFile = File(...),
) -> dict:
    name = _pdf_name(contract)
    data = await contract.read()
    try:
        return {"org_id": org_id.strip(), **submit_contract(data, name, org_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/jobs/{job_id}")
def job_status(job_id: str, org_id: str = Depends(require_company)) -> dict:
    job = get_job(org_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/v1/checks/clause")
def check_one_clause(
    body: ClauseCheck,
    org_id: str = Depends(require_company),
) -> dict:
    try:
        return check_clause(org_id, body.clause_text, body.clause_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
