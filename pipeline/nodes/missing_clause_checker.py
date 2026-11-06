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

from pydantic import BaseModel, Field

from langsmith import traceable

from core.state import AgentState, ClauseComplianceResult
from core.vector_store import get_vector_store
from core.model import get_llm
from core.settings import get_config, get_default_org_id
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


def _obligation_label(requirement) -> str:
    """Use the requirement's own heading when the section contains more than one."""
    first_line = requirement.source_text.strip().split("\n", 1)[0]
    if ":" in first_line:
        label = first_line.split(":", 1)[0]
        label = re.sub(r"^[#*\s\d.]+", "", label).strip(" *")
        label = re.sub(r"^\d+(?:\.\d+)*\s*", "", label).strip()
        words = label.split()
        if label and len(words) <= 12 and len(label) <= 80:
            return label
    return requirement.section_title


class _ObligationAssessment(BaseModel):
    coverage_status: str = Field(
        description="fully_covered, partially_covered, not_covered, contradicted, or ambiguous"
    )
    reason: str
    exact_policy_quote: str | None = None


def _llm_obligation_check_enabled() -> bool:
    return get_config("USE_LLM_OBLIGATION_CHECK", "false").lower() in ("true", "1", "yes")


def _assess_with_model(requirement, contract_clauses: list):
    """Ask the model to compare one policy requirement with the contract.

    Returns None when the check is off or the model call fails. The caller
    then uses the word-overlap result.
    """
    if not _llm_obligation_check_enabled():
        return None
    clause_lines = []
    for clause in contract_clauses:
        clause_type = (
            clause.clause_type if hasattr(clause, "clause_type") else clause.get("clause_type", "")
        ) or ""
        clause_text = (clause.text if hasattr(clause, "text") else clause.get("text", "")) or ""
        clause_lines.append(f"[{clause_type}]\n{clause_text}")
    contract_block = "\n\n".join(clause_lines) or "(No contract clauses were extracted.)"
    prompt = f"""Compare this one policy requirement with the vendor contract.
Use only the text below. Do not apply a special rule for any legal topic.
If the contract narrows a duty that the policy states, the status is contradicted or partially_covered.
If the contract has no language for this requirement, the status is not_covered.
If you cannot tell, the status is ambiguous.
exact_policy_quote must be copied from the policy requirement.

Policy requirement:
{requirement.source_text}

Vendor contract:
{contract_block}
"""
    try:
        model = get_llm(temperature=0.0).with_structured_output(_ObligationAssessment)
        result = model.invoke(prompt)
    except Exception as exc:
        print(f"[Warning] Obligation model check failed: {exc}")
        return None
    status = (result.coverage_status or "").strip().lower().replace("-", "_").replace(" ", "_")
    allowed = {
        "fully_covered",
        "partially_covered",
        "not_covered",
        "contradicted",
        "ambiguous",
    }
    if status not in allowed:
        return None
    verified, cleaned = verify_citation(
        result.exact_policy_quote,
        [Document(page_content=requirement.source_text)],
    )
    if result.exact_policy_quote and not verified:
        status = "ambiguous"
    return status, result.reason, cleaned


def _normalize_clause_text(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _decided_clauses(answers: list) -> list[dict]:
    """Present-clause verdicts the compliance checker has already produced."""
    decided = []
    for answer in answers or []:
        if not isinstance(answer, dict) or answer.get("is_missing_clause"):
            continue
        decided.append({
            "text": _normalize_clause_text(answer.get("clause_text", "")),
            "title": _normalize_clause_text(
                answer.get("clause_title") or answer.get("clause_type") or ""
            ),
            "quote": _normalize_clause_text(answer.get("exact_policy_quote") or ""),
        })
    return [row for row in decided if row["text"] or row["title"]]


def _requirement_already_decided(result, requirement, decided_clauses: list[dict]) -> bool:
    """Skip a second alert when this same obligation was already judged.

    A different atomic obligation in the same section still alerts. That is
    detected by requiring two shared title words, so "Payment" does not hide
    a separate late-penalty requirement.
    """
    source = _normalize_clause_text(getattr(requirement, "source_text", "") or "")
    if len(source) >= 40:
        for row in decided_clauses:
            quote = row.get("quote") or ""
            if len(quote) >= 40 and (quote in source or source in quote):
                return True
    matched = _normalize_clause_text(result.matched_clause_text or "")
    if result.coverage_status != "not_covered" and matched:
        if any(
            row["text"] and (matched in row["text"] or row["text"] in matched)
            for row in decided_clauses
        ):
            return True
    title_tokens = normalize_tokens(requirement.section_title)
    for row in decided_clauses:
        if len(title_tokens & normalize_tokens(row["title"])) >= 2:
            return True
    return False


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
        if res.coverage_status != "fully_covered":
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
        decided_clauses = _decided_clauses(state.get("answer", []))
        for req in requirements:
            res = assess_requirement_coverage(req, contract_clauses, tenant_cfg)
            model_assessment = _assess_with_model(req, contract_clauses)
            if model_assessment is not None:
                res.coverage_status = model_assessment[0]
                res.explanation = model_assessment[1]
            if res.coverage_status != "fully_covered":
                if _requirement_already_decided(res, req, decided_clauses):
                    continue
                doc_name = sec.metadata.get("doc_name", "Company Policy")
                label = _obligation_label(req)
                exact_quote = req.source_text.strip() if len(requirements) > 1 else sec.text.strip()
                citation_verified, cleaned_quote = verify_citation(
                    exact_quote,
                    [Document(page_content=sec.text)],
                )
                if res.coverage_status == "ambiguous":
                    compliance_status = "review_required"
                    reason = (
                        f"Contract evidence for '{label}' needs manual review. "
                        f"{res.explanation}"
                    )
                elif res.coverage_status == "partially_covered":
                    compliance_status = "non_compliant"
                    reason = (
                        f"The vendor contract only partially covers '{label}'. "
                        f"{res.explanation}"
                    )
                elif res.coverage_status == "contradicted":
                    compliance_status = "non_compliant"
                    reason = (
                        f"The vendor contract contradicts '{label}'. "
                        f"{res.explanation}"
                    )
                else:
                    compliance_status = "non_compliant"
                    reason = (
                        f"The policy requirement '{label}' is not in the vendor contract."
                    )
                alert = {
                    "clause_title": f"🚨 MISSING MANDATORY CLAUSE: {label}",
                    "clause_text": "[OMITTED FROM VENDOR CONTRACT]",
                    "policy_source": doc_name,
                    "reason": reason,
                    "compliant": False,
                    "compliance_status": compliance_status,
                    "coverage_status": res.coverage_status,
                    "clauses_internal": [f"{cleaned_quote} — `{sec.breadcrumb}`"],
                    "suggested_revision": (
                        f"Add '{label}' to the vendor contract: \"{cleaned_quote}\""
                    ),
                    "exact_policy_quote": cleaned_quote,
                    "citation_verified": citation_verified,
                    "policy_evidence_status": "topic_found",
                    "policy_requirement_id": req.requirement_id,
                    "is_missing_clause": True,
                }
                missing_alerts.append(alert)

    return {"missing_clauses": missing_alerts}
