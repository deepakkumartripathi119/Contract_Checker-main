"""
pipeline/nodes/missing_clause_checker.py — Bidirectional reverse retrieval for missing clauses.

Purpose
-------
Traditional one-way RAG only evaluates clauses that exist in the vendor contract.
If a vendor completely omits a mandatory company policy clause, one-way RAG never flags it because there is no
contract clause to trigger retrieval.

This node performs Reverse Retrieval using requirement-level obligation coverage:
1. Gathers all mandatory policy sections for the organization.
2. Extracts atomic policy requirements (obligations, entitlements, prohibitions).
3. Checks each requirement against all extracted vendor contract clauses.
4. If a mandatory policy requirement is omitted or only partially covered, it emits an alert:
   "🚨 MISSING MANDATORY CLAUSE: <Section Title>" with non-compliant verdict and
   suggested revision to insert the missing clause.
"""

from __future__ import annotations

import os
import re
import hashlib
from typing import Optional, List

from langsmith import traceable

from core.state import AgentState, ClauseComplianceResult
from core.vector_store import get_vector_store
from core.model import get_config, get_default_org_id
from core.tenant import get_tenant_config
from core.citation_verifier import verify_citation
from core.requirements import (
    PolicyRequirement,
    extract_policy_requirements,
    assess_requirement_coverage,
    is_informational_section,
    normalize_tokens,
)
from langchain.schema import Document


def _policy_requirement_id(section) -> str:
    """Identify one logical policy requirement across parent/child chunks."""
    doc_id = (
        section.metadata.get("doc_id")
        or getattr(section, "doc_id", None)
        or section.metadata.get("doc_name", "policy")
    )
    heading_num = str(section.metadata.get("heading_num", "")).strip()
    heading = str(section.metadata.get("heading", "")).strip().lower()
    if heading_num and heading_num.lower() not in {"flat", "unknown", "#", "##", "###", "####"}:
        if re.search(r"\d", heading_num):
            top_num = heading_num.split(".")[0]
            return f"{doc_id}:{top_num}"
    clean_heading = re.sub(r"[^a-zA-Z0-9]+", "_", heading).strip("_")
    digest = hashlib.sha256(section.text.strip().encode("utf-8")).hexdigest()[:16]
    return f"{doc_id}:{clean_heading}:{digest}"


def _normalize_tokens(text: str) -> set[str]:
    """Tokenize and remove common stop words for topical matching."""
    return normalize_tokens(text)


def is_mandatory_policy_section(title: str, text: str) -> bool:
    """
    Determine if a policy section establishes a mandatory contractual requirement.
    Informational/overview/procedural sections without normative obligation language
    do not require a separate contract clause.
    """
    return not is_informational_section(title, text)


def is_policy_section_covered(
    section_title: str,
    section_text: str,
    contract_clauses: list,
    min_keyword_overlap: int = 1,
) -> bool:
    """
    Check whether any vendor contract clause covers this policy section.
    Evaluates individual requirement coverage.
    """
    reqs = extract_policy_requirements({
        "heading": section_title,
        "text": section_text,
    })
    if not reqs:
        return True

    for req in reqs:
        res = assess_requirement_coverage(req, contract_clauses)
        if res.coverage_status in ("not_covered", "partially_covered", "contradicted"):
            return False
    return True


@traceable
def missing_clause_checker(state: AgentState) -> dict:
    """
    LangGraph node: identify mandatory internal policy sections omitted by the vendor.

    Returns missing clause alerts separately from present-clause verdicts.
    """
    enabled = get_config("ENABLE_MISSING_CLAUSE_CHECK", "true").lower() in ("true", "1", "yes")
    if not enabled:
        return {"answer": []}

    org_id = state.get("org_id", get_default_org_id())
    tenant_cfg = get_tenant_config(org_id)
    store = get_vector_store()
    policy_sections = store.get_policy_sections(org_id)

    if not policy_sections:
        return {"answer": []}

    contract_clauses = state.get("extracted_clauses", [])
    missing_alerts = []
    seen_requirements: set[str] = set()

    for sec in policy_sections:
        # Determine section title from metadata or breadcrumb
        sec_title = sec.metadata.get("heading")
        if not sec_title:
            breadcrumb = sec.breadcrumb or ""
            match = re.search(r"Section\s+[\d\.]*:\s*([^\|\]]+)", breadcrumb)
            sec_title = match.group(1).strip() if match else f"Section {sec.metadata.get('heading_num', '')}".strip()

        if not sec_title or sec_title.lower() in ("flat", "unknown", "section"):
            # Derive from first non-empty line
            lines = [l.strip() for l in sec.text.split("\n") if l.strip()]
            sec_title = lines[0][:40] if lines else "Mandatory Policy Requirement"

        # Check if section is mandatory/normative
        if is_informational_section(sec_title, sec.text):
            continue

        req_id = _policy_requirement_id(sec)
        if req_id in seen_requirements:
            continue
        seen_requirements.add(req_id)

        # Decompose section into atomic policy requirements
        requirements = extract_policy_requirements(sec)
        if not requirements:
            continue

        # Evaluate coverage of each requirement in the section
        for req in requirements:
            res = assess_requirement_coverage(req, contract_clauses, tenant_cfg)
            if res.coverage_status in ("not_covered", "partially_covered", "contradicted"):
                doc_name = sec.metadata.get("doc_name", "Company Policy")
                exact_quote = req.source_text.strip() if len(requirements) > 1 else sec.text.strip()
                citation_verified, cleaned_quote = verify_citation(
                    exact_quote,
                    [Document(page_content=sec.text)],
                )
                alert = {
                    "clause_title": f"🚨 MISSING MANDATORY CLAUSE: {req.section_title}",
                    "clause_text": "[OMITTED FROM VENDOR CONTRACT]",
                    "policy_source": doc_name,
                    "reason": (
                        f"The organization's internal policy requires strict adherence to '{req.section_title}', "
                        f"but this mandatory provision is completely omitted from the vendor contract. "
                        "The vendor contract does not contain the policy requirement."
                    ),
                    "compliant": False,
                    "compliance_status": "non_compliant",
                    "clauses_internal": [f"{cleaned_quote} — `{sec.breadcrumb}`"],
                    "suggested_revision": (
                        f"Insert mandatory '{req.section_title}' clause into vendor contract: "
                        f"\"{cleaned_quote}\""
                    ),
                    "exact_policy_quote": cleaned_quote,
                    "citation_verified": citation_verified,
                    "policy_evidence_status": "topic_found",
                    "policy_requirement_id": req_id,
                    "is_missing_clause": True,
                }
                missing_alerts.append(alert)

    return {"missing_clauses": missing_alerts}
