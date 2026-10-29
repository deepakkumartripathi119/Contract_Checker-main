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
    doc_id = (
        section.metadata.get("doc_id")
        or getattr(section, "doc_id", None)
        or section.metadata.get("doc_name", "policy")
    )
    heading_num = str(section.metadata.get("heading_num", "")).strip()
    heading = str(section.metadata.get("heading", "")).strip().lower()
    if heading_num and heading_num.lower() not in {"flat", "unknown"}:
        top_num = heading_num.split(".")[0]
        return f"{doc_id}:{top_num}"
    digest = hashlib.sha256(section.text.strip().encode("utf-8")).hexdigest()[:16]
    return f"{doc_id}:{heading}:{digest}"


def _normalize_tokens(text: str) -> set[str]:
    """Tokenize and remove common stop words for topical matching."""
    stop_words = {
        "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of",
        "with", "by", "from", "as", "is", "are", "shall", "be", "this", "that",
        "section", "clause", "policy", "all", "any", "must", "will", "terms",
        "provisions", "requirements"
    }
    tokens = set(re.findall(r"\b[a-zA-Z]{3,}\b", text.lower()))
    return tokens - stop_words


def is_mandatory_policy_section(title: str, text: str) -> bool:
    """
    Determine if a policy section establishes a mandatory contractual requirement.
    Informational/overview/procedural sections without normative obligation language
    do not require a separate contract clause.
    """
    informational_patterns = [
        r"^internal policy( document)?$",
        r"^company policy( document)?$",
        r"^procurement policy( document)?$",
        r"^table of contents$",
        r"^overview$",
        r"^introduction$",
        r"^scope$",
        r"^purpose$",
        r"^definitions$",
        r"^version history$",
    ]
    clean_title = re.sub(r"^\d+(\.\d+)*\s*", "", title.strip().lower()).strip("*# ")
    for pat in informational_patterns:
        if re.match(pat, clean_title):
            return False

    normative_pattern = r"\b(shall|must|mandatory|obligated|required|agrees to|will adhere|is prohibited|reserves the right|right to|may audit|entitled to|duty to)\b"
    return bool(re.search(normative_pattern, f"{title} {text}", re.IGNORECASE))


def _evaluate_obligation_coverage(
    policy_title: str,
    policy_text: str,
    clause_type: str,
    clause_text: str,
) -> bool:
    """Evaluate whether a contract clause substantively satisfies a policy obligation."""
    p_lower = f"{policy_title} {policy_text}".lower()
    c_lower = f"{clause_type} {clause_text}".lower()

    # 1. Breach Notification obligation
    is_breach_policy = bool(
        re.search(r"\b(breach|security incident|data compromise|cyber incident)\b", p_lower)
        and re.search(r"\b(notif\w*|inform\w*|report\w*|alert\w*)\b", p_lower)
    )
    if is_breach_policy:
        has_breach_topic = bool(re.search(r"\b(breach|security incident|compromise|unauthorized access|incident)\b", c_lower))
        has_notify_action = bool(re.search(r"\b(notif\w*|inform\w*|report to|alert\w*)\b", c_lower))
        if not (has_breach_topic and has_notify_action):
            return False

        # Deadline check if policy specifies a strict deadline (e.g. 24 hours / 1 day)
        if re.search(r"\b(24\s*hours?|24-hour|24hr|within 24|one day|1 day)\b", p_lower):
            # A weaker deadline in the contract (e.g. 14 days, 30 days) does not satisfy strict 24-hr policy
            if (
                re.search(r"\b(14|15|30|45|60|90)\s*(business\s*)?days?\b", c_lower)
                or re.search(r"\b(2|3|4|5|7|10)\s*weeks?\b", c_lower)
                or re.search(r"\b(48|72)\s*hours?\b", c_lower)
            ):
                return False

            valid_deadline = bool(re.search(r"\b(24\s*hours?|one\s*day|1\s*day|twenty-four\s*hours?|promptly|immediately)\b", c_lower))
            if not valid_deadline:
                days_match = re.search(r"\b(\d+)\s*(business\s*)?days?\b", c_lower)
                if days_match and int(days_match.group(1)) > 1:
                    return False

        return True

    # 2. Audit Rights obligation
    is_audit_policy = bool(
        re.search(r"\baudit\b", p_lower)
        and re.search(r"\b(right|inspect|access|examine|records|books|facilities)\b", p_lower)
    )
    if is_audit_policy:
        is_only_reports = bool(re.search(r"\b(provide audit report|provide annual audit|provide soc|furnish audit|deliver audit report)\b", c_lower))
        has_audit_rights = bool(re.search(r"\b(right to audit|may audit|entitled to audit|inspect books|examine records|inspect facilities|access to records|examine and audit|permit audit|audit vendor)\b", c_lower))
        if is_only_reports and not has_audit_rights:
            return False
        return has_audit_rights or bool(re.search(r"\baudit\b", c_lower))

    # 3. Confidentiality / NDA
    if re.search(r"\b(confidential\w*|non-disclosure|nda)\b", p_lower):
        return bool(re.search(r"\b(confidential\w*|non-disclosure|nda|proprietary information)\b", c_lower))

    # 4. Dispute Resolution / Governing Law
    if re.search(r"\b(dispute\w*|arbitration|litigation|governing law|jurisdiction|venue)\b", p_lower):
        return bool(re.search(r"\b(dispute\w*|arbitration|litigation|governing law|jurisdiction|venue|forum|courts?)\b", c_lower))

    # 5. Indemnification / Liability
    if re.search(r"\b(indemnif\w*|hold harmless|defend\w*)\b", p_lower):
        return bool(re.search(r"\b(indemnif\w*|hold harmless|defend\w*)\b", c_lower))

    # 6. Payment / Invoicing
    if re.search(r"\b(payment\w*|invoic\w*|net\s+\d+|billing)\b", p_lower):
        return bool(re.search(r"\b(payment\w*|invoic\w*|net\s+\d+|billing|fees?|pay\b)\b", c_lower))

    # 7. Insurance
    if re.search(r"\b(insurance|liability coverage|policy limit)\b", p_lower):
        return bool(re.search(r"\b(insurance|coverage|policy limit)\b", c_lower))

    # 8. General open-ended obligation matching
    title_tokens = _normalize_tokens(policy_title)
    if not title_tokens:
        title_tokens = _normalize_tokens(policy_text[:120])
    clause_tokens = _normalize_tokens(f"{clause_type} {clause_text}")
    return bool(title_tokens and len(title_tokens.intersection(clause_tokens)) >= 1)


def is_policy_section_covered(
    section_title: str,
    section_text: str,
    contract_clauses: list,
    min_keyword_overlap: int = 1,
) -> bool:
    """
    Check whether any vendor contract clause covers this policy section.
    """
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

        if _evaluate_obligation_coverage(section_title, section_text, c_type, c_text):
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

        if not is_mandatory_policy_section(sec_title, sec.text):
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
