"""A PDF of the report already stored on a job. No model call."""

from __future__ import annotations


def report_pdf(job: dict) -> bytes:
    import pymupdf

    lines = _lines(job)
    doc = pymupdf.open()
    page = doc.new_page()
    y = 64
    for line in lines:
        if y > 780:
            page = doc.new_page()
            y = 64
        page.insert_text((56, y), line, fontsize=11)
        y += 16
    data = doc.tobytes()
    doc.close()
    return data


def _lines(job: dict) -> list[str]:
    result = job.get("result") or {}
    summary = result.get("summary") or {}
    kind = job.get("kind") or ""
    title = "Your clause has been analyzed." if kind == "clause.submitted" else "Your contract has been analyzed."
    lines = [title, ""]
    if result.get("document_name"):
        lines.append("File: " + str(result["document_name"]))
    lines.append("Total: " + str(summary.get("total") or 0))
    lines.append("Compliant: " + str(summary.get("matches_the_policy") or 0))
    lines.append("Not in the contract: " + str(summary.get("not_in_the_contract") or 0))
    lines.append("Review: " + str(summary.get("needs_a_person_to_review") or 0))
    non_compliant = (
        (summary.get("does_not_match_the_policy") or 0)
        + (summary.get("only_partly_in_the_contract") or 0)
        + (summary.get("conflicts_with_the_policy") or 0)
    )
    lines.append("Non-compliant: " + str(non_compliant))
    lines.append("Time: " + _elapsed(job))
    lines.append("")
    for item in list(result.get("present_clauses") or []) + list(result.get("missing_clauses") or []):
        if not isinstance(item, dict):
            continue
        lines.append(item.get("clause_title") or "Clause")
        lines.append(item.get("reader_label") or "")
        if item.get("clause_text"):
            lines.extend(_wrap("Contract text: " + str(item["clause_text"])))
        reason = item.get("reason") or item.get("review_reason")
        if reason:
            lines.extend(_wrap(str(reason)))
        if item.get("exact_policy_quote"):
            lines.extend(_wrap("Policy quote: " + str(item["exact_policy_quote"])))
        for place in item.get("clauses_internal") or []:
            lines.extend(_wrap("Policy: " + str(place)))
        if item.get("suggested_revision"):
            lines.extend(_wrap("Suggested revision: " + str(item["suggested_revision"])))
        lines.append("")
    return lines


def _elapsed(job: dict) -> str:
    start = job.get("created_at") or ""
    end = job.get("updated_at") or ""
    if not start or not end:
        return ""
    from datetime import datetime

    try:
        began = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return ""
    seconds = max(0, int((finished - began).total_seconds()))
    return f"{seconds} seconds"


def _wrap(text: str) -> list[str]:
    words = text.split()
    lines = []
    current = ""
    for word in words:
        trial = word if not current else current + " " + word
        if len(trial) > 90:
            if current:
                lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines
