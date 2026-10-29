"""
pipeline/nodes/compliance_checker.py — LangGraph node: evaluate clauses against policy.

Phase 4 Features
----------------
1. Two-Tier Cache Check:
   - Before running an expensive LLM evaluation, checks SQLite cache (Tier 1 SHA-256
     and Tier 2 semantic match with numeric and negation guards).
   - If cache hit: returns cached verdict instantly (<1ms, $0 LLM cost).
2. Deterministic Citation Verification:
   - Verifies whether exact_policy_quote provided by the LLM is an authentic verbatim
     substring or contiguous word sequence in the retrieved policy chunks.
   - Annotates citation_verified=True or False. If unverified, adds warning tag to reason.
3. Micro-Batch Evaluation:
   - Groups 4-5 clauses into a single structured prompt for micro_batch_compliance_model.
"""

from __future__ import annotations

import os
import re
from typing import Optional
from langchain.schema import Document

from core.state import SubState, ClauseComplianceResult, MicroBatchComplianceResult
from core.model import (
    compliance_model,
    micro_batch_compliance_model,
    get_config,
    get_default_org_id,
)
from core.cache import get_cache
from core.citation_verifier import verify_compliance_result
from pipeline.nodes.document_parser import remove_synthetic_expected_annotations
from core.vector_store import get_vector_store
from pipeline.nodes.retriever import policy_topic_matches
from prompts.compliance import (
    COMPLIANCE_SYSTEM_PROMPT,
    MICRO_BATCH_COMPLIANCE_SYSTEM_PROMPT,
)


def _combine_docs_with_sources(documents: list[Document]) -> str:
    """Flatten retrieved documents into a single labelled string for the prompt."""
    combined = ""
    for doc in documents:
        source = doc.metadata.get("source", "unknown")
        breadcrumb = doc.metadata.get("breadcrumb", "")
        heading = doc.metadata.get("heading", "")
        content = doc.page_content.strip()
        header = f"Source: {source}"
        if heading:
            header += f" | Policy section: {heading}"
        if breadcrumb:
            header += f" | {breadcrumb}"
        combined += f"{header}\nContent:\n{content}\n\n---\n"
    return combined


def _topic_filtered_docs(clause_type: str, documents: list[Document]) -> list[Document]:
    """Keep retrieved evidence aligned with the clause being evaluated."""
    return [
        doc
        for doc in documents
        if policy_topic_matches(
            clause_type,
            doc.metadata.get("heading", ""),
            f"{doc.metadata.get('breadcrumb', '')} {doc.page_content}",
        )
    ]


def _clause_fields(item: dict) -> tuple[str, str, list[Document]]:
    clause_data = item.get("clause", {})
    clause_text = remove_synthetic_expected_annotations(
        (
            clause_data.get("text", "")
            if isinstance(clause_data, dict)
            else getattr(clause_data, "text", "")
        )
    )
    clause_type = (
        clause_data.get("clause_type", "Unknown")
        if isinstance(clause_data, dict)
        else getattr(clause_data, "clause_type", "Unknown")
    )
    return clause_text, clause_type, _topic_filtered_docs(
        clause_type, item.get("relevant_docs", [])
    )


def _system_error_result(
    item: dict,
    message: str,
) -> dict:
    clause_text, clause_type, docs = _clause_fields(item)
    return {
        "clause_title": clause_type,
        "clause_text": clause_text,
        "policy_source": (
            docs[0].metadata.get("source", "Unknown") if docs else "Unknown"
        ),
        "compliant": False,
        "compliance_status": "review_required",
        "reason": f"SYSTEM ERROR: {message}. Manual review required.",
        "clauses_internal": [],
        "suggested_revision": "SYSTEM_UNAVAILABLE",
        "exact_policy_quote": "SYSTEM_UNAVAILABLE",
        "citation_verified": False,
    }


def _no_policy_result(item: dict, org_id: str | None = None) -> dict:
    clause_text, clause_type, _ = _clause_fields(item)
    resolved_org_id = org_id or item.get("org_id")
    behavior = "compliant"
    if resolved_org_id:
        try:
            from core.tenant import get_tenant_config
            cfg = get_tenant_config(resolved_org_id)
            behavior = cfg.silent_policy_behavior
        except Exception:
            behavior = "compliant"

    if behavior == "review_required":
        return {
            "clause_title": clause_type,
            "clause_text": clause_text,
            "policy_source": "NO INTERNAL POLICY FOUND",
            "reason": (
                "The checked internal policy contains no applicable rule for this clause. "
                "Under configured tenant rules, manual review is required for silent topics."
            ),
            "compliant": False,
            "compliance_status": "review_required",
            "clauses_internal": [],
            "suggested_revision": "REVIEW_REQUIRED",
            "exact_policy_quote": None,
            "citation_verified": None,
            "policy_evidence_status": "silence_verified",
        }

    return {
        "clause_title": clause_type,
        "clause_text": clause_text,
        "policy_source": "NO INTERNAL POLICY FOUND",
        "reason": (
            "The checked internal policy contains no applicable rule for this clause. "
            "Under the silent-policy rule, it is compliant by default."
        ),
        "compliant": True,
        "compliance_status": "compliant",
        "clauses_internal": [],
        "suggested_revision": None,
        "exact_policy_quote": None,
        "citation_verified": None,
        "policy_evidence_status": "silence_verified",
    }


def _retrieval_review_result(item: dict) -> dict:
    clause_text, clause_type, _ = _clause_fields(item)
    return {
        "clause_title": clause_type,
        "clause_text": clause_text,
        "policy_source": "POLICY_RETRIEVAL_FAILED",
        "reason": (
            "Internal policy evidence could not be retrieved or inspected. "
            "Compliance cannot be established; manual review is required."
        ),
        "compliant": False,
        "compliance_status": "review_required",
        "clauses_internal": [],
        "suggested_revision": "SYSTEM_UNAVAILABLE",
        "exact_policy_quote": None,
        "citation_verified": False,
        "policy_evidence_status": "retrieval_failed",
    }


def _normalize_batch_result(
    result: dict,
    item: dict,
    docs: list[Document],
) -> dict:
    status = str(result.get("status", "")).strip().lower()
    if status and status not in {"compliant", "non_compliant", "non-compliant"}:
        raise ValueError(f"batch result has invalid status: {status}")
    compliant = result.get("compliant")
    if compliant is None:
        if status in {"compliant", "non_compliant", "non-compliant"}:
            compliant = status == "compliant"
        else:
            raise ValueError("batch result has no valid status or compliant value")
    if not isinstance(compliant, bool):
        raise ValueError("batch result compliant value is not boolean")

    clause_text, clause_type, _ = _clause_fields(item)
    normalized = {
        "clause_text": clause_text,
        "policy_source": result.get(
            "policy_source",
            docs[0].metadata.get("source", "Unknown") if docs else "Unknown",
        ),
        "reason": result["reason"],
        "compliant": compliant,
        "clauses_internal": result.get("clauses_internal", []),
        "suggested_revision": result.get("suggested_revision"),
        "exact_policy_quote": result.get("exact_policy_quote"),
        "citation_verified": None,
        "clause_title": clause_type,
        "policy_evidence_status": item.get("policy_evidence_status"),
    }
    normalized["compliance_status"] = "compliant" if compliant else "non_compliant"
    return verify_compliance_result(normalized, docs)


def compliance_checker(state: SubState) -> dict:
    """
    LangGraph node: evaluate a single vendor clause against retrieved policy docs.

    Returns the 'answer' key so the parent graph accumulates results
    via the Annotated[List, add] reducer in AgentState.
    """
    # 1. Fast path: if 3-zone gate marked this clause as "skip", preserve the verdict
    if state.get("rerank_status") == "skip" and state.get("answer"):
        return {"answer": state["answer"]}

    clause_data = state.get("clause", {})
    clause_text = remove_synthetic_expected_annotations(
        (
            clause_data.get("text", "")
            if isinstance(clause_data, dict)
            else getattr(clause_data, "text", "")
        )
    )
    clause_type = (
        clause_data.get("clause_type", "Unknown")
        if isinstance(clause_data, dict)
        else getattr(clause_data, "clause_type", "Unknown")
    )
    org_id = state.get("org_id", get_default_org_id())
    from core.tenant import get_tenant_config
    tenant_cfg = get_tenant_config(org_id)
    active_version = tenant_cfg.active_policy_version
    tenant_settings = f"{tenant_cfg.silent_policy_behavior}:{tenant_cfg.favorable_deviation_behavior}"

    cache = get_cache()
    clause_embedding = state.get("clause_embedding")

    # 2. Two-Tier Cache Check (Tier 1 SHA256 & Tier 2 Semantic)
    cached_verdict = cache.get_verdict(
        clause_text,
        org_id,
        clause_type=clause_type,
        version=active_version,
        embedding=clause_embedding,
        tenant_settings=tenant_settings,
    )
    if cached_verdict:
        cached_copy = dict(cached_verdict)
        cached_copy["clause_title"] = clause_type
        cached_copy["clause_text"] = clause_text
        return {"answer": [cached_copy]}

    docs = _topic_filtered_docs(clause_type, state.get("relevant_docs", []))
    docs_str = _combine_docs_with_sources(docs)
    evidence_status = state.get("policy_evidence_status")
    if not evidence_status:
        policy_sections = get_vector_store().get_policy_sections(org_id)
        evidence_status = (
            "retrieval_failed"
            if not policy_sections
            else (
                "topic_found"
                if any(
                    policy_topic_matches(
                        clause_type,
                        section.metadata.get("heading", ""),
                        section.text,
                    )
                    for section in policy_sections
                )
                else "silence_verified"
            )
        )

    # 3. No policy retrieved -> default compliant
    item = {"clause": clause_data, "relevant_docs": docs}
    if not docs_str.strip():
        result = (
            _no_policy_result(item, org_id=org_id)
            if evidence_status in {"searched", "silence_verified"}
            else _retrieval_review_result(item)
        )
        cache.set_verdict(
            org_id,
            clause_text,
            result,
            clause_type=clause_type,
            version=active_version,
            embedding=clause_embedding,
            tenant_settings=tenant_settings,
        )
        return {"answer": [result]}

    system_instruction = COMPLIANCE_SYSTEM_PROMPT.format(company_name=org_id)
    prompt = f"""{system_instruction}

Clause Type: {clause_type}

Clause (proposed by vendor):
---
{clause_text}
---

Internal Policy (retrieved):
---
{docs_str}
---

"""

    try:
        response = compliance_model.invoke(prompt)
    except Exception as e:
        print(f"[ERROR] compliance_model invoke failed: {e}")
        return {"answer": [_system_error_result(
            {"clause": clause_data, "relevant_docs": docs},
            f"LLM provider failed to respond ({e})",
        )]}

    compliance_result = response.model_dump()
    compliance_result["policy_evidence_status"] = evidence_status

    # Deterministic Citation Verification (Phase 4)
    compliance_result = verify_compliance_result(compliance_result, docs)
    compliance_result["clause_title"] = clause_type
    compliance_result["clause_text"] = clause_text

    compliance_result["compliance_status"] = "compliant" if compliance_result.get("compliant") else "non_compliant"
    # Persist in Two-Tier Cache for future requests
    cache.set_verdict(
        org_id,
        clause_text,
        compliance_result,
        clause_type=clause_type,
        version=active_version,
        embedding=clause_embedding,
        tenant_settings=tenant_settings,
    )

    return {"answer": [compliance_result]}


# ---------------------------------------------------------------------------
# Micro-Batch Compliance Evaluation (4-5 clauses in 1 single LLM call)
# ---------------------------------------------------------------------------

def evaluate_micro_batch(
    clauses_with_docs: list[dict],
) -> list[dict]:
    """
    Evaluate a micro-batch of clauses (typically 3-5) in ONE single LLM invocation.
    """
    if not clauses_with_docs:
        return []

    if len(clauses_with_docs) == 1:
        res = compliance_checker(clauses_with_docs[0])
        return res.get("answer", [])

    org_id = (
        clauses_with_docs[0].get("org_id", get_default_org_id())
        if clauses_with_docs
        else get_default_org_id()
    )
    from core.tenant import get_tenant_config
    tenant_cfg = get_tenant_config(org_id)
    active_version = tenant_cfg.active_policy_version
    tenant_settings = f"{tenant_cfg.silent_policy_behavior}:{tenant_cfg.favorable_deviation_behavior}"
    cache = get_cache()

    # Check cache for each item in the batch
    cached_results: list[dict | None] = []
    uncached_indices: list[int] = []

    for i, item in enumerate(clauses_with_docs):
        c_text, c_type, docs = _clause_fields(item)
        cached = cache.get_verdict(
            c_text,
            org_id,
            clause_type=c_type,
            version=active_version,
            embedding=item.get("clause_embedding"),
            tenant_settings=tenant_settings,
        )
        if cached:
            cached_copy = dict(cached)
            cached_copy["clause_title"] = c_type
            cached_copy["clause_text"] = c_text
            cached_results.append(cached_copy)
        else:
            cached_results.append(None)
            uncached_indices.append(i)

    # If all were cached, return immediately!
    if not uncached_indices:
        return [r for r in cached_results if r is not None]

    # Format multi-clause prompt for uncached clauses
    company_name = clauses_with_docs[0].get("org_id", get_default_org_id())
    system_instruction = MICRO_BATCH_COMPLIANCE_SYSTEM_PROMPT.format(
        company_name=company_name
    )
    batch_prompt = f"""{system_instruction}

"""

    for list_pos, idx in enumerate(uncached_indices, start=1):
        item = clauses_with_docs[idx]
        c_text, c_type, docs = _clause_fields(item)
        docs_text = _combine_docs_with_sources(docs)

        batch_prompt += f"""### CLAUSE #{list_pos}: {c_type}
Vendor Proposed Text:
{c_text}

Retrieved Policy Evidence:
{docs_text or "No applicable internal policy retrieved."}

---
"""

    try:
        response = micro_batch_compliance_model.invoke(batch_prompt)
        if not response or not hasattr(response, "results"):
            raise ValueError("micro-batch response did not contain a results array")
        if len(response.results) != len(uncached_indices):
            raise ValueError(
                "micro-batch response count did not match the number of input clauses"
            )

        raw_results = [res_obj.model_dump() for res_obj in response.results]
        result_ids = [result.get("clause_id") for result in raw_results]
        if all(result_id is not None for result_id in result_ids):
            expected_ids = list(range(1, len(uncached_indices) + 1))
            if sorted(result_ids) != expected_ids:
                raise ValueError("micro-batch clause IDs were missing, duplicated, or invalid")
            raw_results = [
                next(result for result in raw_results if result["clause_id"] == clause_id)
                for clause_id in expected_ids
            ]

        for res_dict, idx in zip(raw_results, uncached_indices):
            item = clauses_with_docs[idx]
            c_text, c_type, docs = _clause_fields(item)
            if not docs:
                res_dict = (
                    _no_policy_result(item, org_id=org_id)
                    if item.get("policy_evidence_status") in {
                        "searched",
                        "silence_verified",
                    }
                    else _retrieval_review_result(item)
                )
            else:
                res_dict = _normalize_batch_result(res_dict, item, docs)

            res_dict["clause_title"] = c_type
            res_dict["clause_text"] = c_text

            cache.set_verdict(
                org_id,
                c_text,
                res_dict,
                clause_type=c_type,
                version=active_version,
                embedding=item.get("clause_embedding"),
                tenant_settings=tenant_settings,
            )
            cached_results[idx] = res_dict
        return [r for r in cached_results if r is not None]
    except Exception as e:
        print(f"[ERROR] Micro-batch invoke failed: {e}. Falling back to sequential evaluation.")
        for idx in uncached_indices:
            try:
                res = compliance_checker(clauses_with_docs[idx])
                answers = res.get("answer", [])
                if answers:
                    cached_results[idx] = answers[0]
                else:
                    cached_results[idx] = _system_error_result(
                        clauses_with_docs[idx],
                        f"Compliance check returned no answer ({e})",
                    )
            except Exception as item_err:
                cached_results[idx] = _system_error_result(
                    clauses_with_docs[idx],
                    f"Sequential compliance check failed ({item_err})",
                )
        return [r for r in cached_results if r is not None]
