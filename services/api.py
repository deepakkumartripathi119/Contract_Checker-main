"""HTTP door for the checker.

org_id is not accepted from the request body. It comes from the company login token.
"""

from __future__ import annotations

import socket
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from services.company_login import (
    change_company_account,
    company_from_token,
    company_profile,
    login_company,
    register_company,
)
from core.company_budget import BudgetExceeded, charge_company
from core.jobs import current_policy, get_job, list_contract_jobs
from core.object_store import get_object_store
from core.tenant_repository import get_tenant_repository
from core.vendors import clear_vendor_report, create_vendor, hide_vendor, list_vendors
from services.contract_check import (
    check_clause,
    submit_contract,
    submit_policies,
    submit_vendor_clause,
    submit_vendor_contract,
)
from services.report_pdf import report_pdf

_DASHBOARD = Path(__file__).resolve().parents[1] / "apps" / "dashboard" / "index.html"


app = FastAPI(
    title="Contract Checker",
    version="0.1.0",
    description=(
        "Accepts a policy or a contract, stores the file, and returns a job id. "
        "The worker runs the checker. The organization id comes from the login token."
    ),
)


class VendorCreate(BaseModel):
    name: str = Field(min_length=1)


class ClauseCheck(BaseModel):
    clause_text: str
    clause_type: str


class VendorClause(BaseModel):
    clause_text: str = Field(min_length=1, max_length=10000)
    clause_type: str = Field(min_length=1, max_length=255)


class RegisterCompany(BaseModel):
    org_id: str = ""
    login_name: str = ""
    password: str = Field(min_length=8)
    name: str = ""
    business_email: str = ""
    industry: str = ""
    company_size: str = ""
    headquarters: str = ""
    website: str = ""


class LoginCompany(BaseModel):
    login_name: str
    password: str


class CompanyAccountChange(BaseModel):
    current_password: str
    business_email: str = ""
    new_password: str = ""


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


def require_budget(org_id: str = Depends(require_company)) -> str:
    """Count one work request for this company. Status polls do not come here."""
    try:
        charge_company(org_id)
    except BudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return org_id


def _pdf_name(upload: UploadFile) -> str:
    name = upload.filename or "document.pdf"
    lowered = name.lower()
    if lowered.endswith(".doc") and not lowered.endswith(".docx"):
        raise HTTPException(status_code=400, detail="The old .doc format is not accepted. Save it as .docx.")
    if not lowered.endswith(".pdf") and not lowered.endswith(".docx"):
        raise HTTPException(status_code=400, detail="Only PDF and .docx files are accepted")
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
        token, company_id = register_company(
            body.org_id,
            body.login_name,
            body.password,
            body.name,
            business_email=body.business_email,
            industry=body.industry,
            company_size=body.company_size,
            headquarters=body.headquarters,
            website=body.website,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"token": token, "token_type": "bearer", "org_id": company_id, "name": _company_name(company_id)}


@app.post("/v1/login")
def login(body: LoginCompany) -> dict:
    try:
        token, company_id = login_company(body.login_name, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"token": token, "token_type": "bearer", "org_id": company_id, "name": _company_name(company_id)}


@app.get("/v1/company")
def read_company(org_id: str = Depends(require_company)) -> dict:
    try:
        return company_profile(org_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/v1/company")
def update_company(body: CompanyAccountChange, org_id: str = Depends(require_company)) -> dict:
    try:
        return change_company_account(
            org_id,
            body.current_password,
            body.business_email,
            body.new_password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _company_name(org_id: str) -> str:
    company = get_tenant_repository().get(org_id)
    if company is None or not company.name:
        return org_id
    return company.name


@app.get("/v1/policies/current")
def current_company_policy(org_id: str = Depends(require_company)) -> dict:
    found = current_policy(org_id)
    return {"org_id": org_id, **found}


@app.get("/v1/policies/file")
def download_company_policy(org_id: str = Depends(require_company)) -> Response:
    found = current_policy(org_id).get("file") or {}
    key = found.get("object_key") or ""
    if not key:
        raise HTTPException(status_code=404, detail="No policy file is stored")
    data = get_object_store().get(key)
    filename = found.get("filename") or "policy"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _current_policy_pdf(org_id: str) -> tuple[str, bytes]:
    """The stored policy when it is a PDF. A Word file is refused."""
    found = current_policy(org_id).get("file") or {}
    key = found.get("object_key") or ""
    if not key:
        raise HTTPException(status_code=404, detail="No policy file is stored")
    filename = found.get("filename") or "policy"
    data = get_object_store().get(key)
    if not filename.lower().endswith(".pdf") or not data.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="This file cannot be shown on this page")
    return filename, data


def _policy_words(document) -> list[dict]:
    """Each word and where it sits on its page, from the top-left, as fractions."""
    words = []
    for number, page in enumerate(document, start=1):
        width = float(page.rect.width) or 1
        height = float(page.rect.height) or 1
        for item in page.get_text("words"):
            x0, y0, x1, y1, text = item[:5]
            if not str(text).strip():
                continue
            block = int(item[5]) if len(item) > 5 else 0
            line = int(item[6]) if len(item) > 6 else 0
            words.append({
                "page": number,
                "t": str(text),
                "x": round(float(x0) / width, 4),
                "y": round(float(y0) / height, 4),
                "w": round(max(0.0, float(x1) - float(x0)) / width, 4),
                "h": round(max(0.0, float(y1) - float(y0)) / height, 4),
                "line": block * 1000 + line,
            })
    return words


@app.get("/v1/policies/preview")
def policy_preview(org_id: str = Depends(require_company)) -> dict:
    """Page count and the words on each page. The page images are separate."""
    import pymupdf

    filename, data = _current_policy_pdf(org_id)
    document = pymupdf.open(stream=data, filetype="pdf")
    try:
        return {
            "filename": filename,
            "pages": document.page_count,
            "words": _policy_words(document),
        }
    finally:
        document.close()


@app.get("/v1/policies/preview/{page}")
def policy_preview_page(page: int, org_id: str = Depends(require_company)) -> Response:
    """One page of the current policy PDF, as a PNG. Pages start at 1."""
    import pymupdf

    _filename, data = _current_policy_pdf(org_id)
    document = pymupdf.open(stream=data, filetype="pdf")
    try:
        if page < 1 or page > document.page_count:
            raise HTTPException(status_code=404, detail="That page is not in the policy")
        image = document[page - 1].get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6), alpha=False)
        png = image.tobytes("png")
    finally:
        document.close()
    return Response(content=png, media_type="image/png")


@app.post("/v1/policies")
async def upload_policies(
    org_id: str = Depends(require_budget),
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
    org_id: str = Depends(require_budget),
    contract: UploadFile = File(...),
) -> dict:
    name = _pdf_name(contract)
    data = await contract.read()
    try:
        return {"org_id": org_id.strip(), **submit_contract(data, name, org_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/vendors")
def add_vendor(body: VendorCreate, org_id: str = Depends(require_company)) -> dict:
    try:
        vendor = create_vendor(org_id, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return vendor


@app.get("/v1/vendors")
def company_vendors(org_id: str = Depends(require_company)) -> dict:
    return {"vendors": list_vendors(org_id)}


@app.delete("/v1/vendors/{vendor_id}")
def remove_vendor(vendor_id: str, org_id: str = Depends(require_company)) -> dict:
    """Remove the vendor now. A vendor.deleted job erases its files."""
    if not hide_vendor(org_id, vendor_id):
        raise HTTPException(status_code=404, detail="Vendor not found")
    return {"vendor_id": vendor_id, "deleted": True}


@app.delete("/v1/vendors/{vendor_id}/report")
def remove_vendor_report(vendor_id: str, org_id: str = Depends(require_company)) -> dict:
    """Remove the report now. A report.deleted job erases its file."""
    try:
        job_id = clear_vendor_report(org_id, vendor_id)
    except ValueError as exc:
        missing = str(exc) in {"Vendor not found", "No report is stored"}
        raise HTTPException(status_code=404 if missing else 400, detail=str(exc)) from exc
    return {"vendor_id": vendor_id, "deleted_job_id": job_id}


@app.post("/v1/vendors/{vendor_id}/contract")
async def vendor_contract(
    vendor_id: str,
    org_id: str = Depends(require_budget),
    contract: UploadFile = File(...),
) -> dict:
    name = _pdf_name(contract)
    data = await contract.read()
    try:
        return {"org_id": org_id.strip(), **submit_vendor_contract(org_id, vendor_id, data, name)}
    except ValueError as exc:
        status = 404 if str(exc) == "Vendor not found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.post("/v1/vendors/{vendor_id}/clause")
def vendor_clause(
    vendor_id: str,
    body: VendorClause,
    org_id: str = Depends(require_budget),
) -> dict:
    try:
        return {"org_id": org_id.strip(), **submit_vendor_clause(
            org_id, vendor_id, body.clause_text, body.clause_type
        )}
    except ValueError as exc:
        status = 404 if str(exc) == "Vendor not found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.get("/v1/jobs")
def contract_jobs(org_id: str = Depends(require_company)) -> dict:
    """File name and status for this company's contract checks. The id is for the page, not for display."""
    return {"jobs": list_contract_jobs(org_id)}


@app.get("/v1/jobs/{job_id}")
def job_status(job_id: str, org_id: str = Depends(require_company)) -> dict:
    job = get_job(org_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/v1/jobs/{job_id}/report.pdf")
def download_report(job_id: str, org_id: str = Depends(require_company)) -> Response:
    job = get_job(org_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "succeeded" or not isinstance(job.get("result"), dict):
        raise HTTPException(status_code=400, detail="The report is not ready")
    if "present_clauses" not in job["result"] and "missing_clauses" not in job["result"]:
        raise HTTPException(status_code=400, detail="The report is not ready")
    return Response(
        content=report_pdf(job),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="report.pdf"'},
    )


@app.post("/v1/checks/clause")
def check_one_clause(
    body: ClauseCheck,
    org_id: str = Depends(require_budget),
) -> dict:
    try:
        return check_clause(org_id, body.clause_text, body.clause_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
