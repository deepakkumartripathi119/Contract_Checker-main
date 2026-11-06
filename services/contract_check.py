"""Run the current checker without knowing which screen called it.

The organization id is supplied by the caller. This module does not read a token.
Policy and contract uploads store the file and write a job. The worker runs the check.
"""

from __future__ import annotations

from core.cache import COMPLIANCE_EVALUATION_VERSION, file_hash, get_cache, policy_set_hash
from core.jobs import CLAUSE_SUBMITTED, CONTRACT_SUBMITTED, POLICY_UPLOADED
from core.object_store import get_object_store
from core.report_view import for_the_reader_report
from pipeline.graph import app as contract_graph
from pipeline.nodes.document_parser import leaf_clauses, parse_vendor_contract
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


def _staged_files(org_id: str, kind: str, files: list[tuple[str, bytes]]) -> list[dict]:
    company = _require_org(org_id)
    if not files:
        raise ValueError("At least one PDF is required")
    if any(not data for _, data in files):
        raise ValueError("A file was empty")
    from core.stored_files import stage_original_file

    return [stage_original_file(company, kind, name, data) for name, data in files]


def ingest_policies(files: list[tuple[str, bytes]], org_id: str) -> dict:
    """Store the original policy PDFs, then index them. Returns ids and keys."""
    company = _require_org(org_id)
    stored = _staged_files(company, "policies", files)
    from core.stored_files import record_documents

    record_documents(company, "policies", stored)
    uploads = [Upload(name, data) for name, data in files]
    return {
        "document_ids": process_uploaded_files(uploads, org_id=company),
        "object_keys": [item["object_key"] for item in stored],
    }


def submit_policies(files: list[tuple[str, bytes]], org_id: str) -> dict:
    """Store the policy PDFs and leave a job. The worker indexes them."""
    company = _require_org(org_id)
    stored = _staged_files(company, "policies", files)
    from core.jobs import begin_stored_job

    job_id = begin_stored_job(company, POLICY_UPLOADED, "policies", stored)
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
    return _new_contract_job(company, pdf_bytes, document_name)


def submit_vendor_contract(org_id: str, vendor_id: str, pdf_bytes: bytes, document_name: str) -> dict:
    """Put a contract in one vendor slot.

    The same bytes in this slot open its current check when that check used
    the current policy. After the policy changes, those same bytes are a new
    check. The same bytes in another vendor are a second check. A different
    file replaces the contract this slot shows.
    """
    from core.jobs import _contract_rows, _file_digest
    from core.vendors import SlotTaken, begin_vendor_contract, get_vendor

    company = _require_org(org_id)
    vendor = get_vendor(company, vendor_id)
    if vendor is None:
        raise ValueError("Vendor not found")
    if vendor.get("pending_job_id"):
        pending = _current_vendor_row(_contract_rows(company), vendor.get("pending_job_id"))
        if pending is not None and pending["status"] in ("queued", "running"):
            raise ValueError("A replacement is already running")
    digest = file_hash(pdf_bytes)
    current = _current_vendor_row(_contract_rows(company), vendor.get("job_id"))
    if current is not None and current["status"] != "failed" and _file_digest(current) == digest:
        if current["status"] != "succeeded" or _report_matches_policy(company, current["job_id"]):
            return {
                "job_id": current["job_id"],
                "status": current["status"],
                "kind": CONTRACT_SUBMITTED,
                "vendor_id": vendor_id,
                "existing": True,
            }
    replacing = current is not None and current["status"] == "succeeded"
    if not replacing and vendor.get("job_id"):
        raise ValueError("This vendor already has a submission")
    from core.stored_files import stage_original_file

    staged = stage_original_file(company, "contracts", document_name, pdf_bytes)
    staged["file_hash"] = digest
    try:
        job_id = begin_vendor_contract(company, vendor_id, staged, replacing=replacing)
    except SlotTaken as exc:
        raise ValueError(str(exc)) from exc
    created = {
        "job_id": job_id,
        "status": "queued",
        "kind": CONTRACT_SUBMITTED,
        "vendor_id": vendor_id,
        "object_key": staged["object_key"],
    }
    if replacing:
        created["pending"] = True
    return created


def _current_vendor_row(rows: list[dict], job_id: str | None) -> dict | None:
    if not job_id:
        return None
    for row in rows:
        if row["job_id"] == job_id:
            return row
    return None


def _new_contract_job(company: str, pdf_bytes: bytes, document_name: str) -> dict:
    digest = file_hash(pdf_bytes)
    stored = _staged_files(company, "contracts", [(document_name, pdf_bytes)])
    stored[0]["file_hash"] = digest
    from core.jobs import begin_stored_job

    job_id = begin_stored_job(company, CONTRACT_SUBMITTED, "contracts", stored)
    return {
        "job_id": job_id,
        "status": "queued",
        "kind": CONTRACT_SUBMITTED,
        "object_key": stored[0]["object_key"],
    }


def run_job(job: dict) -> dict:
    """Do the work named on the job. The PDF is read back from object storage."""
    files = job["files"]
    if job["kind"] in ("vendor.deleted", "report.deleted"):
        from core.jobs import apply_delete

        return apply_delete(job)
    if job["kind"] == POLICY_UPLOADED:
        store = get_object_store()
        loaded = [(item, store.get(item["object_key"])) for item in files]
        digest = policy_set_hash([data for _, data in loaded])
        same = bool(digest) and get_cache().get_policy_hash(job["org_id"]) == digest
        uploads = [Upload(item["filename"], data) for item, data in loaded]
        indexed = process_uploaded_files(uploads, org_id=job["org_id"])
        from core.stored_files import keep_only

        keep_only(job["org_id"], "policies", {item["object_key"] for item, _data in loaded})
        return {
            "document_ids": indexed,
            "object_keys": [] if same else [item["object_key"] for item, _data in loaded],
            "policy_unchanged": same,
        }
    if job["kind"] == CONTRACT_SUBMITTED:
        item = files[0]
        return _check_stored_contract(
            get_object_store().get(item["object_key"]),
            item["filename"],
            job["org_id"],
            item["object_key"],
            job["job_id"],
        )
    raise ValueError(f"Unknown job kind '{job['kind']}'")


_REPORT_SHAPE = "leaves-3"


def _report_version(policy_version: str) -> str:
    """A new check shape must not return a report saved by the old shape."""
    return f"{policy_version}:{_REPORT_SHAPE}"


def _policy_version(org_id: str) -> str:
    try:
        from core.tenant import get_tenant_config
        return get_tenant_config(org_id).active_policy_version or COMPLIANCE_EVALUATION_VERSION
    except Exception:
        return COMPLIANCE_EVALUATION_VERSION


def _report_matches_policy(org_id: str, job_id: str) -> bool:
    """True when this finished report was checked against the current policy version."""
    from core.jobs import get_job

    job = get_job(org_id, job_id)
    result = job.get("result") if job else None
    stored = result.get("checked_policy_version") if isinstance(result, dict) else ""
    if not stored:
        return False
    return stored == _policy_version(org_id)


def _check_stored_contract(
    pdf_bytes: bytes,
    document_name: str,
    org_id: str,
    object_key: str,
    job_id: str | None = None,
) -> dict:
    company = _require_org(org_id)
    digest = file_hash(pdf_bytes)
    version = _policy_version(company)
    cached = get_cache().get_report(company, _report_version(version), digest)
    if cached:
        report = dict(cached)
        report["document_name"] = document_name
        report["object_key"] = object_key
        report["report_cache"] = "exact"
        report["checked_policy_version"] = version
        return report
    if job_id:
        from core.jobs import EXTRACTING, set_job_step

        set_job_step(job_id, EXTRACTING)
    clauses, used_llm_fallback = parse_vendor_contract(pdf_bytes, document_name)
    if not clauses:
        raise ValueError("Could not extract any content from the contract PDF")

    if used_llm_fallback:
        state = {"org_id": company, "job_id": job_id or "", "contract": [clauses[0]["text"]]}
    else:
        state = {
            "org_id": company,
            "job_id": job_id or "",
            "extracted_clauses": leaf_clauses(clauses),
        }

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
    report = for_the_reader_report({
        "org_id": company,
        "document_name": document_name,
        "used_llm_fallback": used_llm_fallback,
        "present_clauses": present,
        "missing_clauses": missing,
        "object_key": object_key,
        "source_job_id": job_id,
    })
    report["checked_policy_version"] = version
    get_cache().set_report(company, _report_version(version), digest, report)
    return report


def submit_vendor_clause(org_id: str, vendor_id: str, clause_text: str, clause_type: str) -> dict:
    """Check one clause for this vendor. A replacement waits until this result exists."""
    from core.jobs import _contract_rows
    from core.vendors import (
        SlotTaken,
        claim_vendor_mode,
        clear_vendor_mode,
        get_vendor,
        store_clause_result,
    )

    company = _require_org(org_id)
    heading = (clause_type or "").strip()
    text = (clause_text or "").strip()
    if not heading or not text:
        raise ValueError("clause_text and clause_type are required")
    if len(heading) > 255 or len(text) > 10000:
        raise ValueError("The heading can be 255 characters and the clause 10,000.")
    vendor = get_vendor(company, vendor_id)
    if vendor is None:
        raise ValueError("Vendor not found")
    if vendor.get("pending_job_id"):
        pending = _current_vendor_row(_contract_rows(company), vendor.get("pending_job_id"))
        if pending is not None and pending["status"] in ("queued", "running"):
            raise ValueError("A replacement is already running")
    current = _current_vendor_row(_contract_rows(company), vendor.get("job_id"))
    replacing = current is not None and current["status"] == "succeeded"
    if not replacing and not claim_vendor_mode(company, vendor_id, "clause"):
        raise ValueError("This vendor already has a submission")
    try:
        report = check_clause(company, text, heading)
    except Exception:
        if not replacing:
            clear_vendor_mode(company, vendor_id)
        raise
    report["document_name"] = heading
    old_job = current["job_id"] if replacing and current is not None else ""
    try:
        job_id = store_clause_result(
            company,
            vendor_id,
            heading,
            report,
            replacing=replacing,
            old_job=old_job,
        )
    except SlotTaken as exc:
        raise ValueError(str(exc)) from exc
    return {
        "job_id": job_id,
        "status": "succeeded",
        "kind": CLAUSE_SUBMITTED,
        "vendor_id": vendor_id,
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
    report = for_the_reader_report({
        "org_id": company,
        "present_clauses": present,
        "missing_clauses": [],
        "missing_clause_check": "not_run",
    })
    report["checked_policy_version"] = _policy_version(company)
    return report
