"""Run the current checker without knowing which screen called it.

The organization id is supplied by the caller. This module does not read a token.
Policy and contract uploads store the file and write a job. The worker runs the check.
"""

from __future__ import annotations

from core.jobs import CONTRACT_SUBMITTED, POLICY_UPLOADED, create_job
from core.object_store import get_object_store
from core.stored_files import keep_original_file
from pipeline.graph import app as contract_graph
from pipeline.nodes.document_parser import parse_vendor_contract
from pipeline.nodes.retriever import process_uploaded_files
from pipeline.subgraph import sub_graph


class Upload:
    """File-like object the policy ingester already expects."""

    def __init__(self, name: str, data: bytes):
        self.name = name
        self._data = data

    def read(self) -> bytes:
        return self._data


def _require_org(org_id: str) -> str:
    cleaned = (org_id or "").strip()
    if not cleaned:
        raise ValueError("org_id is required")
    return cleaned


def _plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _dedupe_missing(missing: list[dict]) -> list[dict]:
    kept = []
    seen = set()
    for item in missing:
        key = item.get("policy_requirement_id") or item.get("clause_title")
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept


def _stored_files(org_id: str, kind: str, files: list[tuple[str, bytes]]) -> list[dict]:
    company = _require_org(org_id)
    if not files:
        raise ValueError("At least one PDF is required")
    if any(not data for _, data in files):
        raise ValueError("A file was empty")
    return [
        keep_original_file(company, kind, name, data)
        for name, data in files
    ]


def ingest_policies(files: list[tuple[str, bytes]], org_id: str) -> dict:
    """Store the original policy PDFs, then index them. Returns ids and keys."""
    company = _require_org(org_id)
    stored = _stored_files(company, "policies", files)
    uploads = [Upload(name, data) for name, data in files]
    return {
        "document_ids": process_uploaded_files(uploads, org_id=company),
        "object_keys": [item["object_key"] for item in stored],
    }


def submit_policies(files: list[tuple[str, bytes]], org_id: str) -> dict:
    """Store the policy PDFs and leave a job. The worker indexes them."""
    company = _require_org(org_id)
    stored = _stored_files(company, "policies", files)
    job_id = create_job(company, POLICY_UPLOADED, stored)
    return {
        "job_id": job_id,
        "status": "queued",
        "kind": POLICY_UPLOADED,
        "document_ids": [item["document_id"] for item in stored],
        "object_keys": [item["object_key"] for item in stored],
    }


def submit_contract(pdf_bytes: bytes, document_name: str, org_id: str) -> dict:
    """Store one contract and leave a job. The worker writes the report."""
    company = _require_org(org_id)
    stored = _stored_files(company, "contracts", [(document_name, pdf_bytes)])
    job_id = create_job(company, CONTRACT_SUBMITTED, stored)
    return {
        "job_id": job_id,
        "status": "queued",
        "kind": CONTRACT_SUBMITTED,
        "object_key": stored[0]["object_key"],
    }


def run_job(job: dict) -> dict:
    """Do the work named on the job. The PDF is read back from object storage."""
    files = job["files"]
    if job["kind"] == POLICY_UPLOADED:
        uploads = [
            Upload(item["filename"], get_object_store().get(item["object_key"]))
            for item in files
        ]
        return {
            "document_ids": process_uploaded_files(uploads, org_id=job["org_id"]),
            "object_keys": [item["object_key"] for item in files],
        }
    if job["kind"] == CONTRACT_SUBMITTED:
        item = files[0]
        return _check_stored_contract(
            get_object_store().get(item["object_key"]),
            item["filename"],
            job["org_id"],
            item["object_key"],
        )
    raise ValueError(f"Unknown job kind '{job['kind']}'")


def _check_stored_contract(pdf_bytes: bytes, document_name: str, org_id: str, object_key: str) -> dict:
    company = _require_org(org_id)
    clauses, used_llm_fallback = parse_vendor_contract(pdf_bytes, document_name)
    if not clauses:
        raise ValueError("Could not extract any content from the contract PDF")

    if used_llm_fallback:
        state = {"org_id": company, "contract": [clauses[0]["text"]]}
    else:
        state = {"org_id": company, "extracted_clauses": clauses}

    response = contract_graph.invoke(
        state,
        config={
            "max_concurrency": 5,
            "tags": ["api", "vendor_contract_audit"],
            "metadata": {
                "document_name": document_name,
                "org_id": company,
                "total_clauses_extracted": 1 if used_llm_fallback else len(clauses),
            },
        },
    )
    present = _plain(response.get("answer") or [])
    missing = _dedupe_missing(_plain(response.get("missing_clauses") or []))
    if not isinstance(present, list):
        present = [present]
    return {
        "org_id": company,
        "document_name": document_name,
        "used_llm_fallback": used_llm_fallback,
        "present_clauses": present,
        "missing_clauses": missing,
        "object_key": object_key,
    }


def check_clause(org_id: str, clause_text: str, clause_type: str) -> dict:
    """Check one pasted clause. This path does not look for missing clauses."""
    company = _require_org(org_id)
    text = (clause_text or "").strip()
    heading = (clause_type or "").strip()
    if not text or not heading:
        raise ValueError("clause_text and clause_type are required")

    response = sub_graph.invoke(
        {
            "org_id": company,
            "clause": {"text": text, "clause_type": heading, "metadata": {}},
        },
        config={
            "tags": ["api", "manual_clause_audit"],
            "metadata": {
                "document_name": "manual_clause_input",
                "org_id": company,
                "total_clauses_extracted": 1,
            },
        },
    )
    present = _plain(response.get("answer") or [])
    if not isinstance(present, list):
        present = [present]
    return {
        "org_id": company,
        "present_clauses": present,
        "missing_clauses": [],
        "missing_clause_check": "not_run",
    }
