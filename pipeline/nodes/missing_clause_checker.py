"""
pipeline/nodes/missing_clause_checker.py — Bidirectional reverse retrieval for missing clauses.

Purpose
-------
Traditional one-way RAG only evaluates clauses that exist in the vendor contract.
If a vendor completely OMITS a mandatory company policy clause (e.g., Confidentiality,
Indemnification, or Audit Rights), one-way RAG never flags it because there is no
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
from typing import Optional

from core.state import AgentState, ClauseComplianceResult
from core.vector_store import get_vector_store
from core.model import get_config


def _normalize_tokens(text: str) -> set[str]:
    """Tokenize and remove common stop words for topical matching."""
    stop_words = {
        "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of",
        "with", "by", "from", "as", "is", "are", "shall", "be", "this", "that",
        "section", "clause", "policy", "nexify", "solutions", "all", "any"
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


def missing_clause_checker(state: AgentState) -> dict:
    """
    LangGraph node: identify mandatory internal policy sections omitted by the vendor.

    Appends missing clause alerts to AgentState["answer"].
    """
    enabled = get_config("ENABLE_MISSING_CLAUSE_CHECK", "true").lower() in ("true", "1", "yes")
    if not enabled:
        return {"answer": []}

    org_id = get_config("POLICY_ORG_ID", "nexify")
    store = get_vector_store()
    policy_sections = store.get_policy_sections(org_id)

    if not policy_sections:
        return {"answer": []}

    contract_clauses = state.get("extracted_clauses", [])
    missing_alerts = []

    for sec in policy_sections:
        # Determine section title from metadata or breadcrumb
        sec_title = sec.metadata.get("heading")
        if not sec_title:
            breadcrumb = sec.breadcrumb or ""
            # e.g. "[Doc: Nexify Policy | Section 1: Payment Terms | Page 1]"
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
            alert = {
                "clause_title": f"🚨 MISSING MANDATORY CLAUSE: {sec_title}",
                "clause_text": "[OMITTED FROM VENDOR CONTRACT]",
                "policy_source": doc_name,
                "reason": (
                    f"Nexify Solutions internal policy requires strict adherence to '{sec_title}', "
                    f"but this mandatory provision is completely omitted from the vendor contract. "
                    f"Executing this contract without this clause exposes Nexify to unmitigated risk."
                ),
                "compliant": False,
                "clauses_internal": [f"{sec.text[:250]}... — `{sec.breadcrumb}`"],
                "suggested_revision": (
                    f"Insert mandatory '{sec_title}' clause into vendor contract: "
                    f"\"{sec.text.strip()}\""
                ),
                "exact_policy_quote": sec.text[:150],
                "citation_verified": True,
                "is_missing_clause": True,
            }
            missing_alerts.append(alert)

    return {"answer": missing_alerts}
