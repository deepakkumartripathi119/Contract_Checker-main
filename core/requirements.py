"""
core/requirements.py — Requirement-based policy assessment models and evaluation.

Decomposes policy sections into atomic normative requirements (mandatory obligations,
prohibitions, entitlements) and evaluates multi-requirement coverage against contract
clauses, eliminating brittle category-specific branches and section-level false coverage.
"""

from __future__ import annotations

import re
import hashlib
from typing import Optional, List, Any
from pydantic import BaseModel, Field

from core.tenant import TenantConfig, get_tenant_config


class PolicyRequirement(BaseModel):
    """An atomic normative obligation extracted from a policy section."""
    requirement_id: str = Field(..., description="Stable, deterministic requirement identifier")
    doc_id: str = Field(default="policy", description="Source policy document identifier")
    section_title: str = Field(..., description="Section title or heading")
    source_text: str = Field(..., description="Verbatim text of the requirement")
    breadcrumb: str = Field(default="", description="Hierarchical breadcrumb or location in policy")
    normative_nature: str = Field(
        default="mandatory",
        description="Normative character: mandatory, prohibited, entitlement, advisory, informational, ambiguous",
    )
    obligated_party: Optional[str] = Field(default=None, description="Party holding the obligation")
    action: Optional[str] = Field(default=None, description="Obligated or prohibited action/subject")
    conditions: Optional[str] = Field(default=None, description="Conditions or qualifiers")
    threshold_value: Optional[float] = Field(default=None, description="Normalized numeric threshold")
    threshold_unit: Optional[str] = Field(default=None, description="Unit: usd, days, hours, percent, etc.")
    threshold_direction: Optional[str] = Field(default=None, description="Direction: min, max, exact")
    exceptions: Optional[str] = Field(default=None, description="Exceptions or carve-outs")


class RequirementCoverageResult(BaseModel):
    """The result of assessing contract coverage for an individual policy requirement."""
    requirement_id: str
    coverage_status: str  # fully_covered, partially_covered, not_covered, contradicted, ambiguous
    matched_clause_type: Optional[str] = None
    matched_clause_text: Optional[str] = None
    explanation: str = ""


_INFORMATIONAL_TITLES = [
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

_NORMATIVE_PATTERN = re.compile(
    r"\b(shall|must|mandatory|obligated|required|agrees to|will adhere|is prohibited|shall not|must not|may not|reserves the right|right to|may audit|entitled to|duty to)\b",
    re.IGNORECASE,
)

_STOP_WORDS = {
    "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of",
    "with", "by", "from", "as", "is", "are", "shall", "be", "this", "that",
    "section", "clause", "policy", "all", "any", "must", "will", "terms",
    "provisions", "requirements", "vendor", "contractor", "parties", "party",
    "agreement", "nexify", "solutions", "customer", "company", "organization",
    "entity", "services", "applicable", "including", "standard", "general",
}


def normalize_tokens(text: str) -> set[str]:
    """Tokenize and remove common stop words and company tokens for topical matching."""
    tokens = set(re.findall(r"\b[a-zA-Z]{3,}\b", text.lower()))
    return tokens - _STOP_WORDS


def is_informational_section(title: str, text: str) -> bool:
    """Check if section is purely informational or structural without normative obligations."""
    clean_title = re.sub(r"^\d+(\.\d+)*\s*", "", title.strip().lower()).strip("*# ")
    for pat in _INFORMATIONAL_TITLES:
        if re.match(pat, clean_title):
            return True
    return not bool(_NORMATIVE_PATTERN.search(f"{title} {text}"))


def extract_policy_requirements(chunk: Any) -> list[PolicyRequirement]:
    """
    Parse a policy chunk into one or more atomic PolicyRequirement instances.
    Extracts distinct obligations within a section (e.g. cyber + general liability).
    """
    title = ""
    text = ""
    breadcrumb = ""
    doc_id = "policy"
    heading_num = ""

    if hasattr(chunk, "text"):
        text = chunk.text
        title = getattr(chunk, "metadata", {}).get("heading", "") or getattr(chunk, "heading", "")
        breadcrumb = getattr(chunk, "breadcrumb", "") or ""
        doc_id = getattr(chunk, "doc_id", None) or getattr(chunk, "metadata", {}).get("doc_id", "policy")
        heading_num = str(getattr(chunk, "metadata", {}).get("heading_num", "")).strip()
    elif isinstance(chunk, dict):
        text = chunk.get("text", "")
        title = chunk.get("metadata", {}).get("heading", "") or chunk.get("heading", "")
        breadcrumb = chunk.get("breadcrumb", "")
        doc_id = chunk.get("metadata", {}).get("doc_id", "policy")
        heading_num = str(chunk.get("metadata", {}).get("heading_num", "")).strip()

    if not title:
        match = re.search(r"Section\s+[\d\.]*:\s*([^\|\]]+)", breadcrumb)
        title = match.group(1).strip() if match else (heading_num or "Policy Section")

    if is_informational_section(title, text):
        return []

    base_num = ""
    if heading_num and heading_num.lower() not in {"flat", "unknown", "#", "##", "###", "####"}:
        if re.search(r"\d", heading_num):
            base_num = heading_num.split(".")[0]

    # Check for numbered sub-sections like 1.1, 1.2 or 4.1, 4.2
    sub_sections = re.findall(r"(?:^|\n)(?:\d+\.\d+|[A-Z]\.)\s+([^\n]+(?:\n(?!\d+\.\d+|[A-Z]\.)[^\n]+)*)", text)
    if len(sub_sections) > 1:
        req_texts = [s.strip() for s in sub_sections if _NORMATIVE_PATTERN.search(s)]
    else:
        req_texts = [text.strip()]

    requirements: list[PolicyRequirement] = []
    total = len(req_texts)

    for idx, r_text in enumerate(req_texts, start=1):
        if base_num:
            if total == 1:
                req_id = f"{doc_id}:{base_num}"
            else:
                req_id = f"{doc_id}:{base_num}.{idx}"
        else:
            clean_title_slug = re.sub(r"[^a-zA-Z0-9]+", "_", title.lower()).strip("_")
            digest = hashlib.sha256(r_text.strip().encode("utf-8")).hexdigest()[:16]
            req_id = f"{doc_id}:{clean_title_slug}:{digest}"

        nature = "mandatory"
        if re.search(r"\b(prohibit|shall not|must not|may not|is prohibited|no .* permitted)\b", r_text, re.I):
            nature = "prohibited"
        elif re.search(r"\b(reserves the right|right to|entitled to|may audit)\b", r_text, re.I):
            nature = "entitlement"
        elif re.search(r"\b(should|recommended|advisory)\b", r_text, re.I):
            nature = "advisory"

        requirements.append(
            PolicyRequirement(
                requirement_id=req_id,
                doc_id=doc_id,
                section_title=title,
                source_text=r_text,
                breadcrumb=breadcrumb,
                normative_nature=nature,
            )
        )

    return requirements


def assess_requirement_coverage(
    requirement: PolicyRequirement,
    contract_clauses: list[dict | Any],
    tenant_cfg: Optional[TenantConfig] = None,
) -> RequirementCoverageResult:
    """
    Assess whether contract clauses satisfy, partially satisfy, or contradict a policy requirement.
    Uses obligation-aware matching without brittle category hardcoding.
    """
    req_text = requirement.source_text.lower()
    req_title = requirement.section_title.lower()
    p_combined = f"{req_title} {req_text}"
    req_tokens = normalize_tokens(p_combined)

    # 1. Breach Notification obligation
    is_breach_policy = bool(
        re.search(r"\b(breach|security incident|data compromise|cyber incident)\b", p_combined)
        and re.search(r"\b(notif\w*|inform\w*|report\w*|alert\w*)\b", p_combined)
    )
    if is_breach_policy:
        for clause in contract_clauses:
            c_type = (clause.clause_type if hasattr(clause, "clause_type") else clause.get("clause_type", "")) or ""
            c_text = (clause.text if hasattr(clause, "text") else clause.get("text", "")) or ""
            c_combined = f"{c_type} {c_text}".lower()

            has_breach_topic = bool(re.search(r"\b(breach|security incident|compromise|unauthorized access|incident)\b", c_combined))
            has_notify_action = bool(re.search(r"\b(notif\w*|inform\w*|report\w*|alert\w*)\b", c_combined))

            if not (has_breach_topic and has_notify_action):
                continue

            # Check deadline if policy has strict deadline
            if re.search(r"\b(24\s*hours?|24-hour|24hr|within 24|one day|1 day)\b", p_combined):
                # Weaker contract deadline fails strict policy
                if (
                    re.search(r"\b(14|15|30|45|60|90)\s*(business\s*)?days?\b", c_combined)
                    or re.search(r"\b(2|3|4|5|7|10)\s*weeks?\b", c_combined)
                    or re.search(r"\b(48|72)\s*hours?\b", c_combined)
                ):
                    return RequirementCoverageResult(
                        requirement_id=requirement.requirement_id,
                        coverage_status="partially_covered",
                        matched_clause_type=c_type,
                        matched_clause_text=c_text,
                        explanation="Contract notification deadline exceeds policy requirement.",
                    )

                valid_deadline = bool(re.search(r"\b(24\s*hours?|one\s*day|1\s*day|twenty-four\s*hours?|promptly|immediately)\b", c_combined))
                if not valid_deadline:
                    days_match = re.search(r"\b(\d+)\s*(business\s*)?days?\b", c_combined)
                    if days_match and int(days_match.group(1)) > 1:
                        return RequirementCoverageResult(
                            requirement_id=requirement.requirement_id,
                            coverage_status="partially_covered",
                            matched_clause_type=c_type,
                            matched_clause_text=c_text,
                            explanation="Contract notification deadline exceeds policy requirement.",
                        )

            return RequirementCoverageResult(
                requirement_id=requirement.requirement_id,
                coverage_status="fully_covered",
                matched_clause_type=c_type,
                matched_clause_text=c_text,
                explanation="Breach notification obligation covered.",
            )

        return RequirementCoverageResult(
            requirement_id=requirement.requirement_id,
            coverage_status="not_covered",
            explanation="No contract clause covers breach notification obligation.",
        )

    # 2. Audit Rights obligation
    is_audit_policy = bool(
        re.search(r"\baudit\b", p_combined)
        and re.search(r"\b(right|inspect|access|examine|records|books|facilities)\b", p_combined)
    )
    if is_audit_policy:
        for clause in contract_clauses:
            c_type = (clause.clause_type if hasattr(clause, "clause_type") else clause.get("clause_type", "")) or ""
            c_text = (clause.text if hasattr(clause, "text") else clause.get("text", "")) or ""
            c_combined = f"{c_type} {c_text}".lower()

            is_only_reports = bool(
                re.search(
                    r"\b(provide audit report|provide annual audit|provide soc|furnish audit|deliver audit report)\b",
                    c_combined,
                )
            )
            has_audit_rights = bool(
                re.search(
                    r"\b(right to audit|may audit|entitled to audit|inspect books|examine records|inspect facilities|access to records|examine and audit|permit audit|audit vendor)\b",
                    c_combined,
                )
            )
            if is_only_reports and not has_audit_rights:
                continue

            if has_audit_rights or bool(re.search(r"\baudit\b", c_combined)):
                return RequirementCoverageResult(
                    requirement_id=requirement.requirement_id,
                    coverage_status="fully_covered",
                    matched_clause_type=c_type,
                    matched_clause_text=c_text,
                    explanation="Audit rights obligation covered.",
                )

        return RequirementCoverageResult(
            requirement_id=requirement.requirement_id,
            coverage_status="not_covered",
            explanation="No contract clause grants required audit and inspection rights.",
        )

    # 3. Confidentiality / NDA
    if re.search(r"\b(confidential\w*|non-disclosure|nda)\b", p_combined):
        for clause in contract_clauses:
            c_type = (clause.clause_type if hasattr(clause, "clause_type") else clause.get("clause_type", "")) or ""
            c_text = (clause.text if hasattr(clause, "text") else clause.get("text", "")) or ""
            c_combined = f"{c_type} {c_text}".lower()
            if re.search(r"\b(confidential\w*|non-disclosure|nda|proprietary information)\b", c_combined):
                return RequirementCoverageResult(
                    requirement_id=requirement.requirement_id,
                    coverage_status="fully_covered",
                    matched_clause_type=c_type,
                    matched_clause_text=c_text,
                    explanation="Confidentiality obligation covered.",
                )
        return RequirementCoverageResult(
            requirement_id=requirement.requirement_id,
            coverage_status="not_covered",
            explanation="No contract clause covers confidentiality obligation.",
        )

    # 4. Dispute Resolution / Governing Law
    if re.search(r"\b(dispute\w*|arbitration|litigation|governing law|jurisdiction|venue)\b", p_combined):
        for clause in contract_clauses:
            c_type = (clause.clause_type if hasattr(clause, "clause_type") else clause.get("clause_type", "")) or ""
            c_text = (clause.text if hasattr(clause, "text") else clause.get("text", "")) or ""
            c_combined = f"{c_type} {c_text}".lower()
            if re.search(r"\b(dispute\w*|arbitration|litigation|governing law|jurisdiction|venue|forum|courts?)\b", c_combined):
                return RequirementCoverageResult(
                    requirement_id=requirement.requirement_id,
                    coverage_status="fully_covered",
                    matched_clause_type=c_type,
                    matched_clause_text=c_text,
                    explanation="Dispute resolution / governing law obligation covered.",
                )
        return RequirementCoverageResult(
            requirement_id=requirement.requirement_id,
            coverage_status="not_covered",
            explanation="No contract clause covers dispute resolution.",
        )

    # 5. Indemnification / Liability
    if re.search(r"\b(indemnif\w*|hold harmless|defend\w*)\b", p_combined):
        for clause in contract_clauses:
            c_type = (clause.clause_type if hasattr(clause, "clause_type") else clause.get("clause_type", "")) or ""
            c_text = (clause.text if hasattr(clause, "text") else clause.get("text", "")) or ""
            c_combined = f"{c_type} {c_text}".lower()
            if re.search(r"\b(indemnif\w*|hold harmless|defend\w*)\b", c_combined):
                return RequirementCoverageResult(
                    requirement_id=requirement.requirement_id,
                    coverage_status="fully_covered",
                    matched_clause_type=c_type,
                    matched_clause_text=c_text,
                    explanation="Indemnification obligation covered.",
                )
        return RequirementCoverageResult(
            requirement_id=requirement.requirement_id,
            coverage_status="not_covered",
            explanation="No contract clause covers indemnification.",
        )

    # 6. Payment / Invoicing
    if re.search(r"\b(payment\w*|invoic\w*|net\s+\d+|billing)\b", p_combined):
        for clause in contract_clauses:
            c_type = (clause.clause_type if hasattr(clause, "clause_type") else clause.get("clause_type", "")) or ""
            c_text = (clause.text if hasattr(clause, "text") else clause.get("text", "")) or ""
            c_combined = f"{c_type} {c_text}".lower()
            if re.search(r"\b(payment\w*|invoic\w*|net\s+\d+|billing|fees?|pay\b)\b", c_combined):
                return RequirementCoverageResult(
                    requirement_id=requirement.requirement_id,
                    coverage_status="fully_covered",
                    matched_clause_type=c_type,
                    matched_clause_text=c_text,
                    explanation="Payment obligation covered.",
                )
        return RequirementCoverageResult(
            requirement_id=requirement.requirement_id,
            coverage_status="not_covered",
            explanation="No contract clause covers payment terms.",
        )

    # 7. Insurance
    if re.search(r"\b(insurance|liability coverage|policy limit)\b", p_combined):
        for clause in contract_clauses:
            c_type = (clause.clause_type if hasattr(clause, "clause_type") else clause.get("clause_type", "")) or ""
            c_text = (clause.text if hasattr(clause, "text") else clause.get("text", "")) or ""
            c_combined = f"{c_type} {c_text}".lower()
            if re.search(r"\b(insurance|coverage|policy limit)\b", c_combined):
                return RequirementCoverageResult(
                    requirement_id=requirement.requirement_id,
                    coverage_status="fully_covered",
                    matched_clause_type=c_type,
                    matched_clause_text=c_text,
                    explanation="Insurance obligation covered.",
                )
        return RequirementCoverageResult(
            requirement_id=requirement.requirement_id,
            coverage_status="not_covered",
            explanation="No contract clause covers insurance requirements.",
        )

    # 8. General Open-Ended Obligation Matching (lexical token overlap)
    title_tokens = normalize_tokens(requirement.section_title)
    if not title_tokens:
        title_tokens = normalize_tokens(requirement.source_text[:120])

    for clause in contract_clauses:
        c_type = (clause.clause_type if hasattr(clause, "clause_type") else clause.get("clause_type", "")) or ""
        c_text = (clause.text if hasattr(clause, "text") else clause.get("text", "")) or ""
        clause_tokens = normalize_tokens(f"{c_type} {c_text}")
        if title_tokens and len(title_tokens.intersection(clause_tokens)) >= 1:
            return RequirementCoverageResult(
                requirement_id=requirement.requirement_id,
                coverage_status="fully_covered",
                matched_clause_type=c_type,
                matched_clause_text=c_text,
                explanation=f"Obligation matched contract clause '{c_type}'.",
            )

    return RequirementCoverageResult(
        requirement_id=requirement.requirement_id,
        coverage_status="not_covered",
        explanation="No contract clause covers this mandatory requirement.",
    )
