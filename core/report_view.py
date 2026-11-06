"""The report a person reads.

compliant=False is used both for a real mismatch and for "a person must look".
The status field is the one that decides. A missing status is not called a mismatch.
"""

from __future__ import annotations

import re

_STATUSES = {"compliant", "non_compliant", "review_required"}
_NOT_A_QUOTE = {"SYSTEM_UNAVAILABLE", "REVIEW_REQUIRED"}
_NOT_CONTRACT_TEXT = {"[OMITTED FROM VENDOR CONTRACT]"}
_NOT_A_DOCUMENT = {"NO INTERNAL POLICY FOUND", "POLICY_RETRIEVAL_FAILED"}
_TAG = re.compile(
    r"^(?:\[CITATION UNVERIFIED - Missing Quote\]|\[CITATION UNVERIFIED\]|SYSTEM ERROR:)\s*"
)
_REPEATED_REASON = (
    "The checked internal policy contains no applicable rule for this clause.",
    "Internal policy evidence could not be retrieved or inspected.",
    "Reranker confidence",
)
_TITLE_PREFIX = re.compile(
    r"^(?:🚨\s*)?(?:MISSING MANDATORY CLAUSE|Missing):\s*",
    re.IGNORECASE,
)
_BACKTICK = re.compile(r"`([^`]*)`")
_LEADING_NUMBER = re.compile(r"^\d+(?:\.\d+)*\.?\s+")
_SUMMARY_PHRASES = (
    ("Matches the policy", "matches_the_policy", "1 matches the policy", "{n} match the policy"),
    ("Does not match the policy", "does_not_match_the_policy", "1 does not match the policy", "{n} do not match the policy"),
    ("Not in the contract", "not_in_the_contract", "1 is not in the contract", "{n} are not in the contract"),
    ("Only partly in the contract", "only_partly_in_the_contract", "1 is only partly in the contract", "{n} are only partly in the contract"),
    ("Conflicts with the policy", "conflicts_with_the_policy", "1 conflicts with the policy", "{n} conflict with the policy"),
    ("Needs a person to review", "needs_a_person_to_review", "1 needs a person to review", "{n} need a person to review"),
)


def for_the_reader(item: dict) -> dict:
    """Return one finding with a status a client can trust."""
    if not isinstance(item, dict):
        return item
    status = item.get("compliance_status")
    if status not in _STATUSES:
        status = "compliant" if item.get("compliant") is True else "review_required"
    quote = item.get("exact_policy_quote")
    if not isinstance(quote, str) or not quote.strip() or quote.strip() in _NOT_A_QUOTE:
        quote = None
    if item.get("citation_verified") is False:
        status = "review_required"
        quote = None
    reason_text = item.get("reason") if isinstance(item.get("reason"), str) else ""
    if status == "compliant" and "silent policy" in reason_text.lower():
        status = "review_required"
        quote = None
    shown = dict(item)
    shown["compliance_status"] = status
    shown["compliant"] = status == "compliant"
    shown["exact_policy_quote"] = quote
    shown["clause_title"] = _reader_title(item.get("clause_title"))
    shown["clause_text"] = _reader_contract_text(item.get("clause_text"), shown["clause_title"])
    shown["policy_source"] = _reader_source(item.get("policy_source"))
    shown["clauses_internal"] = _reader_locations(item)
    shown["suggested_revision"] = _reader_revision(item, status)
    shown["needs_review"] = status == "review_required"
    shown["review_reason"] = _review_reason(item, status)
    shown["reason"] = _reader_reason(item.get("reason"), shown["review_reason"])
    shown["is_missing_clause"] = bool(item.get("is_missing_clause"))
    shown["reader_label"] = _reader_label(shown)
    return shown


def _reader_label(shown: dict) -> str:
    """The status line both screens show."""
    if shown.get("needs_review"):
        return "Needs a person to review"
    if shown.get("is_missing_clause") and shown.get("compliance_status") == "non_compliant":
        coverage = shown.get("coverage_status")
        if coverage == "contradicted":
            return "Conflicts with the policy"
        if coverage == "partially_covered":
            return "Only partly in the contract"
        return "Not in the contract"
    if shown.get("compliance_status") == "non_compliant":
        return "Does not match the policy"
    return "Matches the policy"


def _plain_reason(reason):
    """Drop the machine tag. Keep the sentence that follows it."""
    if not isinstance(reason, str):
        return reason
    text = _TAG.sub("", reason.strip()).strip()
    return text or None


def _reader_reason(reason, review_reason: str | None):
    """The review sentence already says this. Do not print it twice."""
    text = _plain_reason(reason)
    if review_reason and isinstance(text, str) and text.startswith(_REPEATED_REASON):
        return None
    return text


def _review_reason(item: dict, status: str) -> str | None:
    """One sentence for a person. A match or a mismatch does not need it."""
    if status != "review_required":
        return None
    quote = item.get("exact_policy_quote")
    placeholder = isinstance(quote, str) and quote.strip() in _NOT_A_QUOTE
    reason = item.get("reason")
    if isinstance(reason, str) and "silent policy" in reason.lower():
        return "The company policy has no rule for this topic."
    if isinstance(reason, str) and reason.strip().startswith("SYSTEM ERROR"):
        return "The check could not finish, so a person needs to look."
    if item.get("citation_verified") is False and isinstance(quote, str) and quote.strip() and not placeholder:
        return "The quoted policy text was not found in the policy."
    evidence = item.get("policy_evidence_status")
    if evidence == "silence_verified":
        return "The company policy has no rule for this topic."
    if evidence == "retrieval_failed":
        return "The policy search did not finish, so this cannot be decided."
    return "A person needs to look at this before it is called a match or a mismatch."


def _reader_contract_text(text, title=None):
    """A missing requirement is not a sentence from the contract.

    The title is already shown. A first line that only repeats it is not
    part of the sentence.
    """
    if not isinstance(text, str) or not text.strip() or text.strip() in _NOT_CONTRACT_TEXT:
        return None
    lines = text.strip().splitlines()
    first = _LEADING_NUMBER.sub("", lines[0].strip()).strip()
    if isinstance(title, str) and title.strip() and first.casefold() == title.strip().casefold():
        text = "\n".join(lines[1:]).strip()
    else:
        text = text.strip()
    return text or None


def _reader_source(source):
    """A status string is not the name of a policy file."""
    if not isinstance(source, str) or not source.strip() or source.strip() in _NOT_A_DOCUMENT:
        return None
    return source.strip()


def _reader_locations(item: dict) -> list:
    """The policy place, without the quote or the bracket tag."""
    if item.get("citation_verified") is False:
        return []
    entries = item.get("clauses_internal")
    if not isinstance(entries, list):
        return []
    places = []
    seen = set()
    for entry in entries:
        place = _one_location(entry)
        if place and place not in seen:
            seen.add(place)
            places.append(place)
    return places


def _one_location(entry):
    if not isinstance(entry, str):
        return None
    match = _BACKTICK.search(entry)
    if match is None:
        return None
    return _plain_location(match.group(1))


def _plain_location(raw: str):
    text = raw.strip()
    if not text or text in _NOT_A_QUOTE or text in _NOT_A_DOCUMENT or text in _NOT_CONTRACT_TEXT:
        return None
    if text.startswith("[Doc:") and text.endswith("]"):
        parts = []
        for piece in text[1:-1].split("|"):
            piece = piece.strip()
            if piece.lower().startswith("doc:"):
                name = piece.split(":", 1)[1].strip()
                if name:
                    parts.append(name)
            elif piece.lower().startswith("page "):
                number = piece[5:].strip()
                if number:
                    parts.append(f"page {number}")
            elif piece:
                parts.append(piece)
        return ", ".join(parts) or None
    return text


def _reader_title(title):
    """Drop the machine prefix. Keep the requirement name."""
    if not isinstance(title, str):
        return title
    return _TITLE_PREFIX.sub("", title.strip()).strip()


def _reader_revision(item: dict, status: str):
    """A review is not a rewrite. Placeholders are not a rewrite either."""
    if status == "review_required":
        return None
    revision = item.get("suggested_revision")
    if not isinstance(revision, str) or not revision.strip() or revision.strip() in _NOT_A_QUOTE:
        return None
    return revision.strip()


def summarize_findings(present=None, missing=None) -> dict:
    """Count the labels a person already sees."""
    items = list(present or []) + list(missing or [])
    counts = {key: 0 for _, key, _, _ in _SUMMARY_PHRASES}
    counts["total"] = len(items)
    by_label = {label: key for label, key, _, _ in _SUMMARY_PHRASES}
    for item in items:
        if not isinstance(item, dict):
            continue
        label = item.get("reader_label") or "Needs a person to review"
        key = by_label.get(label, "needs_a_person_to_review")
        counts[key] += 1
    counts["sentence"] = _summary_sentence(counts)
    return counts


def _summary_sentence(counts: dict) -> str:
    if not counts.get("total"):
        return "No findings."
    parts = []
    for _, key, one, many in _SUMMARY_PHRASES:
        n = counts.get(key) or 0
        if n == 1:
            parts.append(one)
        elif n > 1:
            parts.append(many.format(n=n))
    return ". ".join(parts) + "." if parts else "No findings."


def for_the_reader_list(items) -> list:
    if not isinstance(items, list):
        items = [items]
    return [for_the_reader(item) for item in items]


def for_the_reader_report(report: dict) -> dict:
    """Present, missing, and a count of each label."""
    if not isinstance(report, dict):
        return report
    present = for_the_reader_list(report.get("present_clauses") or [])
    missing = for_the_reader_list(report.get("missing_clauses") or [])
    shown = dict(report)
    shown["present_clauses"] = present
    shown["missing_clauses"] = missing
    shown["summary"] = summarize_findings(present, missing)
    return shown
