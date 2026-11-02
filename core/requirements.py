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

from core.tenant import TenantConfig


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
    r"^signatures?$",
    r"^header$",
    r"^footer$",
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
    """Skip contents pages, signatures, and empty sections.

    A missing word such as "shall" or "must" does not make a section informational.
    Customers write obligations in many ways.
    """
    clean_title = re.sub(r"^\d+(\.\d+)*\s*", "", title.strip().lower()).strip("*# ")
    for pat in _INFORMATIONAL_TITLES:
        if re.match(pat, clean_title):
            return True
    return not text.strip()


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
        req_texts = [s.strip() for s in sub_sections if s.strip()]
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


_DURATION_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(business\s+days?|hours?|days?|weeks?|months?)\b",
    re.IGNORECASE,
)
_PROHIBITED_RE = re.compile(
    r"\b(shall not|must not|may not|is prohibited|prohibited from)\b",
    re.IGNORECASE,
)
_MANDATORY_RE = re.compile(
    r"\b(shall|must|required|mandatory|agrees to)\b",
    re.IGNORECASE,
)
_MAX_DURATION_RE = re.compile(
    r"\b(within|no later than|not to exceed|not more than)\b",
    re.IGNORECASE,
)
_MIN_DURATION_RE = re.compile(
    r"\b(at least|minimum of|no less than|not less than)\b",
    re.IGNORECASE,
)
_MIN_THRESHOLD_RE = re.compile(
    r"\b(at least|minimum|no less than|not less than)\b",
    re.IGNORECASE,
)
_MAX_THRESHOLD_RE = re.compile(
    r"\b(at most|not to exceed|not more than|up to)\b",
    re.IGNORECASE,
)
_LARGE_NUMBER_RE = re.compile(
    r"\$\s*\d[\d,]*(?:\.\d+)?|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b"
)


def _clause_parts(clause: dict | Any) -> tuple[str, str]:
    clause_type = (
        clause.clause_type if hasattr(clause, "clause_type") else clause.get("clause_type", "")
    ) or ""
    clause_text = (clause.text if hasattr(clause, "text") else clause.get("text", "")) or ""
    return clause_type, clause_text


def _polarity(text: str) -> str:
    if _PROHIBITED_RE.search(text):
        return "prohibited"
    if _MANDATORY_RE.search(text):
        return "mandatory"
    return "unspecified"


def _durations_in_hours(text: str) -> list[float]:
    durations: list[float] = []
    for number, unit in _DURATION_RE.findall(text):
        value = float(number)
        normalized = unit.lower()
        if "hour" in normalized:
            durations.append(value)
        elif "week" in normalized:
            durations.append(value * 24 * 7)
        elif "month" in normalized:
            durations.append(value * 24 * 30)
        elif "day" in normalized:
            durations.append(value * 24)
    return durations


def _duration_relation(policy_text: str, contract_text: str) -> str:
    """Compare the first stated duration without assuming a legal topic."""
    policy_durations = _durations_in_hours(policy_text)
    contract_durations = _durations_in_hours(contract_text)
    if not policy_durations or not contract_durations:
        return "unknown"
    policy_value = policy_durations[0]
    contract_value = contract_durations[0]
    if abs(policy_value - contract_value) < 1e-6:
        return "compatible"
    policy_is_max = bool(_MAX_DURATION_RE.search(policy_text))
    policy_is_min = bool(_MIN_DURATION_RE.search(policy_text))
    if policy_is_max and not policy_is_min:
        return "weaker" if contract_value > policy_value else "stricter"
    if policy_is_min and not policy_is_max:
        return "weaker" if contract_value < policy_value else "stricter"
    return "unclear"


def _large_numbers(text: str) -> list[float]:
    values: list[float] = []
    for raw in _LARGE_NUMBER_RE.findall(text):
        values.append(float(raw.replace("$", "").replace(",", "").strip()))
    return values


def _numeric_threshold_relation(policy_text: str, contract_text: str) -> str:
    """Compare explicit large numeric thresholds without using a topic list."""
    policy_values = _large_numbers(policy_text)
    contract_values = _large_numbers(contract_text)
    if not policy_values or not contract_values:
        return "unknown"
    policy_value = max(policy_values)
    contract_value = max(contract_values)
    if _MIN_THRESHOLD_RE.search(policy_text) and contract_value < policy_value:
        return "weaker"
    if _MAX_THRESHOLD_RE.search(policy_text) and contract_value > policy_value:
        return "weaker"
    return "unknown"


def assess_requirement_coverage(
    requirement: PolicyRequirement,
    contract_clauses: list[dict | Any],
    tenant_cfg: Optional[TenantConfig] = None,
) -> RequirementCoverageResult:
    """
    Compare one policy requirement with contract clauses.

    The same structural checks apply to every topic: shared obligation language,
    normative polarity, and explicit durations. A single shared token is not
    enough to call a requirement fully covered. tenant_cfg is accepted so
    callers can pass tenant context; this comparison does not override the
    requirement text with tenant-specific legal categories.
    """
    del tenant_cfg
    requirement_tokens = normalize_tokens(requirement.source_text)
    if len(requirement_tokens) < 2:
        requirement_tokens |= normalize_tokens(requirement.section_title)

    best: tuple[int, float, str, str, bool, str] | None = None
    for clause in contract_clauses:
        clause_type, clause_text = _clause_parts(clause)
        combined = f"{clause_type} {clause_text}"
        shared = requirement_tokens & normalize_tokens(combined)
        if not shared:
            continue
        ratio = len(shared) / max(1, len(requirement_tokens))
        polarity_conflict = False
        requirement_polarity = _polarity(requirement.source_text)
        clause_polarity = _polarity(combined)
        if (
            requirement_polarity != "unspecified"
            and clause_polarity != "unspecified"
            and requirement_polarity != clause_polarity
        ):
            polarity_conflict = True
        relation = _duration_relation(requirement.source_text, combined)
        candidate = (len(shared), ratio, clause_type, clause_text, polarity_conflict, relation)
        if best is None or (candidate[0], candidate[1]) > (best[0], best[1]):
            best = candidate

    if best is None:
        return RequirementCoverageResult(
            requirement_id=requirement.requirement_id,
            coverage_status="not_covered",
            explanation="No contract clause covers this requirement.",
        )

    shared_count, ratio, clause_type, clause_text, polarity_conflict, relation = best
    matched = {
        "requirement_id": requirement.requirement_id,
        "matched_clause_type": clause_type,
        "matched_clause_text": clause_text,
    }
    # A handful of shared words is not evidence that this requirement was addressed.
    if shared_count < 2 or ratio < 0.25:
        return RequirementCoverageResult(
            requirement_id=requirement.requirement_id,
            coverage_status="not_covered",
            explanation="No contract clause covers this requirement.",
        )

    if polarity_conflict and shared_count >= 2:
        return RequirementCoverageResult(
            **matched,
            coverage_status="contradicted",
            explanation="Contract language reverses the requirement's obligation.",
        )
    if relation == "weaker":
        return RequirementCoverageResult(
            **matched,
            coverage_status="ambiguous",
            explanation=(
                "The contract states a different duration, and the requirement "
                "does not show who that deadline benefits."
            ),
        )
    threshold_relation = _numeric_threshold_relation(
        requirement.source_text,
        f"{clause_type} {clause_text}",
    )
    if threshold_relation == "weaker" and shared_count >= 2:
        return RequirementCoverageResult(
            **matched,
            coverage_status="partially_covered",
            explanation="Contract numeric threshold is weaker than the requirement.",
        )
    if relation == "unclear":
        return RequirementCoverageResult(
            **matched,
            coverage_status="ambiguous",
            explanation="Both texts state a duration, but the requirement does not say whether it is a minimum or a maximum.",
        )
    if shared_count >= 2 and ratio >= 0.34 and relation in {"compatible", "stricter", "unknown"}:
        return RequirementCoverageResult(
            **matched,
            coverage_status="fully_covered",
            explanation="Contract language covers the requirement without a weaker threshold or reversed obligation.",
        )
    if shared_count >= 2 and ratio >= 0.25:
        return RequirementCoverageResult(
            **matched,
            coverage_status="partially_covered",
            explanation="Contract language overlaps the requirement but does not cover it fully.",
        )
    return RequirementCoverageResult(
        **matched,
        coverage_status="ambiguous",
        explanation="Contract language shares too little of the requirement to decide coverage.",
    )
