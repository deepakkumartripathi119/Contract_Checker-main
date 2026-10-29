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
from typing import Optional
from langchain.schema import Document

from core.state import SubState, ClauseComplianceResult, MicroBatchComplianceResult
from core.model import compliance_model, micro_batch_compliance_model, get_config
from core.cache import get_cache
from core.citation_verifier import verify_compliance_result


def _combine_docs_with_sources(documents: list[Document]) -> str:
    """Flatten retrieved documents into a single labelled string for the prompt."""
    combined = ""
    for doc in documents:
        source = doc.metadata.get("source", "unknown")
        breadcrumb = doc.metadata.get("breadcrumb", "")
        content = doc.page_content.strip()
        header = f"Source: {source}"
        if breadcrumb:
            header += f" | {breadcrumb}"
        combined += f"{header}\nContent:\n{content}\n\n---\n"
    return combined


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
    clause_text = (
        clause_data.get("text", "")
        if isinstance(clause_data, dict)
        else getattr(clause_data, "text", "")
    )
    clause_type = (
        clause_data.get("clause_type", "Unknown")
        if isinstance(clause_data, dict)
        else getattr(clause_data, "clause_type", "Unknown")
    )
    org_id = get_config("POLICY_ORG_ID", "nexify")
    cache = get_cache()
    clause_embedding = state.get("clause_embedding")

    # 2. Two-Tier Cache Check (Tier 1 SHA256 & Tier 2 Semantic)
    cached_verdict = cache.get_verdict(clause_text, org_id, embedding=clause_embedding)
    if cached_verdict:
        cached_verdict["clause_title"] = clause_type
        return {"answer": [cached_verdict]}

    docs = state.get("relevant_docs", [])
    docs_str = _combine_docs_with_sources(docs)

    # 3. No policy retrieved -> default compliant
    if not docs_str.strip():
        result = {
            "clause_title": clause_type,
            "clause_text": clause_text,
            "policy_source": "NO INTERNAL POLICY FOUND",
            "reason": (
                "No internal policy guidelines found for this clause type. "
                "Marked compliant by default."
            ),
            "compliant": True,
            "clauses_internal": [],
            "suggested_revision": None,
            "exact_policy_quote": None,
            "citation_verified": None,
        }
        cache.set_verdict(org_id, clause_text, result, embedding=clause_embedding)
        return {"answer": [result]}

    prompt = f"""You are a contract compliance assistant for Nexify Solutions.

Your task: evaluate whether the vendor-proposed clause below complies with
Nexify Solutions' internal policy guidelines.

Always evaluate strictly from Nexify Solutions' perspective (they are the
recipient of vendor services).

Clause Type: {clause_type}

Clause (proposed by vendor):
---
{clause_text}
---

Internal Policy (retrieved):
---
{docs_str}
---

Evaluation Rules:
1. If the clause matches internal policy → COMPLIANT.
2. If it matches but lacks key points → COMPLIANT, but set suggested_revision.
3. If it deviates:
   - Harms Nexify (unfavorable terms, reduced rights, increased costs) → NON_COMPLIANT
   - Benefits Nexify (better terms, more rights, cost savings) → COMPLIANT
4. If no applicable policy is found → COMPLIANT, policy_source = "NO INTERNAL POLICY FOUND"
5. If non-compliant → suggest a concise revision in suggested_revision.
6. Provide exact verbatim quotes in exact_policy_quote when applicable.

Respond in the exact JSON schema provided. Keep reason and suggested_revision concise.
"""

    try:
        response = compliance_model.invoke(prompt)
    except Exception as e:
        print(f"[Warning] compliance_model invoke failed: {e}")
        response = None

    if response is None:
        compliance_result = {
            "clause_text": clause_text,
            "policy_source": "none",
            "reason": "LLM returned no response; marked compliant by default.",
            "compliant": True,
            "clauses_internal": [],
            "suggested_revision": None,
            "exact_policy_quote": None,
            "citation_verified": None,
        }
    else:
        compliance_result = response.model_dump()

    # Deterministic Citation Verification (Phase 4)
    compliance_result = verify_compliance_result(compliance_result, docs)
    compliance_result["clause_title"] = clause_type

    # Persist in Two-Tier Cache for future requests
    cache.set_verdict(org_id, clause_text, compliance_result, embedding=clause_embedding)

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

    org_id = get_config("POLICY_ORG_ID", "nexify")
    cache = get_cache()

    # Check cache for each item in the batch
    cached_results: list[dict | None] = []
    uncached_indices: list[int] = []

    for i, item in enumerate(clauses_with_docs):
        c_data = item.get("clause", {})
        c_text = c_data.get("text", "") if isinstance(c_data, dict) else getattr(c_data, "text", "")
        cached = cache.get_verdict(
            c_text, org_id, embedding=item.get("clause_embedding")
        )
        if cached:
            c_type = c_data.get("clause_type", "Unknown") if isinstance(c_data, dict) else getattr(c_data, "clause_type", "Unknown")
            cached["clause_title"] = c_type
            cached_results.append(cached)
        else:
            cached_results.append(None)
            uncached_indices.append(i)

    # If all were cached, return immediately!
    if not uncached_indices:
        return [r for r in cached_results if r is not None]

    # Format multi-clause prompt for uncached clauses
    batch_prompt = """You are a contract compliance assistant for Nexify Solutions.
Evaluate the following vendor-proposed clauses against their respective retrieved internal policy guidelines.
Always evaluate strictly from Nexify Solutions' perspective.

"""

    for list_pos, idx in enumerate(uncached_indices, start=1):
        item = clauses_with_docs[idx]
        c_data = item.get("clause", {})
        c_text = c_data.get("text", "") if isinstance(c_data, dict) else getattr(c_data, "text", "")
        c_type = c_data.get("clause_type", "Unknown") if isinstance(c_data, dict) else getattr(c_data, "clause_type", "Unknown")
        docs_text = _combine_docs_with_sources(item.get("relevant_docs", []))

        batch_prompt += f"""### CLAUSE #{list_pos}: {c_type}
Vendor Proposed Text:
{c_text}

Retrieved Policy Evidence:
{docs_text or "No applicable internal policy retrieved."}

---
"""

    batch_prompt += """
For EACH clause above, provide a compliance result in order.
Return the full list of results matching the input clauses.
"""

    llm_results = []
    try:
        response = micro_batch_compliance_model.invoke(batch_prompt)
        if response and hasattr(response, "results"):
            for res_obj, idx in zip(response.results, uncached_indices):
                res_dict = res_obj.model_dump()
                item = clauses_with_docs[idx]
                c_data = item.get("clause", {})
                c_type = c_data.get("clause_type", "Unknown") if isinstance(c_data, dict) else getattr(c_data, "clause_type", "Unknown")
                c_text = c_data.get("text", "") if isinstance(c_data, dict) else getattr(c_data, "text", "")

                # Citation verification
                res_dict = verify_compliance_result(res_dict, item.get("relevant_docs", []))
                res_dict["clause_title"] = c_type

                # Cache result
                cache.set_verdict(
                    org_id, c_text, res_dict,
                    embedding=item.get("clause_embedding"),
                )
                cached_results[idx] = res_dict
            return [r for r in cached_results if r is not None]
    except Exception as e:
        print(f"[Warning] Micro-batch compliance failed: {e}. Falling back to sequential evaluation.")

    # Fallback for remaining uncached
    for idx in uncached_indices:
        res = compliance_checker(clauses_with_docs[idx])
        answers = res.get("answer", [])
        if answers:
            cached_results[idx] = answers[0]

    return [r for r in cached_results if r is not None]
