"""
pipeline/nodes/missing_clause_checker.py — Bidirectional reverse retrieval for missing clauses.

Purpose
-------
Traditional one-way RAG only evaluates clauses that exist in the vendor contract.
If a vendor completely omits a mandatory company policy clause, one-way RAG never flags it because there is no
contract clause to trigger retrieval.

This node performs Reverse Retrieval:
1. Gathers all mandatory policy sections for the organization.
2. Checks each mandatory policy section against all extracted vendor contract clauses.
3. If a mandatory policy rule has zero coverage in the vendor contract, it emits an alert:
   "🚨 MISSING MANDATORY CLAUSE: <Section Title>" with non-compliant verdict and
   suggested revision to insert the missing clause.
"""

from __future__ import annotations

import os
import re
import hashlib
from typing import Optional

from langsmith import traceable

from core.state import AgentState, ClauseComplianceResult
from core.vector_store import get_vector_store
from core.model import get_config, get_default_org_id
from core.citation_verifier import verify_citation
from langchain.schema import Document


def _policy_requirement_id(section) -> str:
    """Identify one logical policy requirement across parent/child chunks."""
    doc_id = section.metadata.get("doc_id") or section.doc_id or section.metadata.get(
        "doc_name", "policy"
    )
    heading_num = str(section.metadata.get("heading_num", "")).strip()
    heading = str(section.metadata.get("heading", "")).strip().lower()
    if heading_num and heading_num.lower() not in {"flat", "unknown"}:
        return f"{doc_id}:{heading_num}"
    digest = hashlib.sha256(section.text.strip().encode("utf-8")).hexdigest()[:16]
    return f"{doc_id}:{heading}:{digest}"


def _normalize_tokens(text: str) -> set[str]:
    """Tokenize and remove common stop words for topical matching."""
    stop_words = {
        "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of",
        "with", "by", "from", "as", "is", "are", "shall", "be", "this", "that",
        "section", "clause", "policy", "all", "any"
    }
    tokens = set(re.findall(r"\b[a-zA-Z]{3,}\b", text.lower()))
    return tokens - stop_words


def is_policy_section_covered(
    section_title: str,
    section_text: str,
    contract_clauses: list,
    min_keyword_overlap: int = 1,
) -> bool:
    """
    Check whether any vendor contract clause covers this policy section.
    """
    title_tokens = _normalize_tokens(section_title)
    if not title_tokens:
        title_tokens = _normalize_tokens(section_text[:100])

    for clause in contract_clauses:
        c_type = (
            clause.clause_type
            if hasattr(clause, "clause_type")
            else clause.get("clause_type", "")
        ) or ""
        c_text = (
            clause.text
            if hasattr(clause, "text")
            else clause.get("text", "")
        ) or ""

        # Check clause title tokens
        c_title_tokens = _normalize_tokens(c_type)
        if title_tokens.intersection(c_title_tokens):
            return True

        # Check clause text tokens
        c_text_tokens = _normalize_tokens(c_text)
        overlap = title_tokens.intersection(c_text_tokens)
        if len(overlap) >= min_keyword_overlap:
            return True

    return False


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
    store = get_vector_store()
    policy_sections = store.get_policy_sections(org_id)

    if not policy_sections:
        return {"answer": []}

    contract_clauses = state.get("extracted_clauses", [])
    missing_alerts = []
    seen_requirements: set[str] = set()

    for sec in policy_sections:
        requirement_id = _policy_requirement_id(sec)
        if requirement_id in seen_requirements:
            continue
        seen_requirements.add(requirement_id)
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

        if sec_title.lower() in {
            "internal policy document",
            "company policy",
            "policy document",
        }:
            continue

        covered = is_policy_section_covered(sec_title, sec.text, contract_clauses)
        if not covered:
            doc_name = sec.metadata.get("doc_name", "Company Policy")
            exact_quote = sec.text.strip()
            citation_verified, cleaned_quote = verify_citation(
                exact_quote,
                [Document(page_content=sec.text)],
            )
            alert = {
                "clause_title": f"🚨 MISSING MANDATORY CLAUSE: {sec_title}",
                "clause_text": "[OMITTED FROM VENDOR CONTRACT]",
                "policy_source": doc_name,
                "reason": (
                    f"The organization's internal policy requires strict adherence to '{sec_title}', "
                    f"but this mandatory provision is completely omitted from the vendor contract. "
                    "The vendor contract does not contain the policy requirement."
                ),
                "compliant": False,
                "clauses_internal": [f"{cleaned_quote} — `{sec.breadcrumb}`"],
                "suggested_revision": (
                    f"Insert mandatory '{sec_title}' clause into vendor contract: "
                    f"\"{cleaned_quote}\""
                ),
                "exact_policy_quote": cleaned_quote,
                "citation_verified": citation_verified,
                "policy_evidence_status": "topic_found",
                "policy_requirement_id": requirement_id,
                "is_missing_clause": True,
            }
            missing_alerts.append(alert)

    return {"missing_clauses": missing_alerts}
