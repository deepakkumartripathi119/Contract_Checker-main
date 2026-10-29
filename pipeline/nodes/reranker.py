"""
pipeline/nodes/reranker.py — LangGraph node: rerank retrieved policy documents with 3-Zone Gate.

Phase 3 Modernization: 3-Zone Confidence Gate
---------------------------------------------
1. Zone 3: score < RERANKER_SKIP_THRESHOLD (default 0.15)
   Deterministic skip: No relevant policy applies to this clause.
   Outputs a compliant-by-default verdict immediately, skipping the expensive LLM
   compliance check entirely (0 LLM tokens, <1ms).
   rerank_status = "skip"

2. Zone 2: RERANKER_SKIP_THRESHOLD <= score < RERANKER_RETRY_THRESHOLD (default 0.15 <= score < 0.35)
   Ambiguous / Low-confidence: We found weak candidates, but query reformulation may help.
   If retry_count < 1: triggers fallback query_generator -> document_retriever loop.
   If already retried: proceeds to compliance check with best available context.
   rerank_status = "retry" or "proceed"

3. Zone 1: score >= RERANKER_RETRY_THRESHOLD (default >= 0.35)
   High confidence: Strong policy evidence found.
   Passes top-K relevant docs to compliance_checker.
   rerank_status = "proceed"
"""

from __future__ import annotations

import os
from langchain.schema import Document
from core.state import SubState
from core.model import get_config
from pipeline.nodes.document_parser import clean_extracted_text


def _make_skip_verdict(
    clause_type: str,
    clause_text: str,
    score: float,
    threshold: float,
) -> dict:
    """Build a deterministic compliance verdict for irrelevant clauses (Zone 3)."""
    return {
        "clause_title": clause_type,
        "clause_text": clause_text,
        "policy_source": "NO INTERNAL POLICY FOUND",
        "reason": (
            f"Reranker confidence ({score:.4f}) is below applicability threshold ({threshold:.4f}). "
            "No internal policy guidelines found for this clause type. "
            "Marked compliant by default."
        ),
        "compliant": True,
        "clauses_internal": [],
        "suggested_revision": None,
        "exact_policy_quote": None,
        "policy_evidence_status": "silence_verified",
    }


def _make_retrieval_review_verdict(
    clause_type: str,
    clause_text: str,
) -> dict:
    return {
        "clause_title": clause_type,
        "clause_text": clause_text,
        "policy_source": "POLICY_RETRIEVAL_FAILED",
        "reason": (
            "Internal policy evidence could not be retrieved or inspected. "
            "Compliance cannot be established; manual review is required."
        ),
        "compliant": False,
        "clauses_internal": [],
        "suggested_revision": "SYSTEM_UNAVAILABLE",
        "exact_policy_quote": None,
        "citation_verified": False,
        "policy_evidence_status": "retrieval_failed",
    }


def Reranker(state: SubState) -> dict:
    """
    LangGraph node: rerank retrieved docs and evaluate 3-zone confidence gate.

    Returns dict updating:
      - relevant_docs: list of top Documents
      - rerank_status: "proceed" | "retry" | "skip"
      - rerank_score : float
      - answer       : [skip_verdict] if status == "skip"
    """
    docs: list[Document] = state.get("retrived_docs", [])
    clause_data = state.get("clause", {})
    clause_text = clean_extracted_text(
        clause_data.get("text", "")
        if isinstance(clause_data, dict)
        else getattr(clause_data, "text", "")
    )
    clause_type = (
        clause_data.get("clause_type", "Unknown")
        if isinstance(clause_data, dict)
        else getattr(clause_data, "clause_type", "Unknown")
    )

    skip_threshold = float(get_config("RERANKER_SKIP_THRESHOLD", "0.15"))
    retry_threshold = float(get_config("RERANKER_RETRY_THRESHOLD", "0.35"))
    top_k = int(get_config("RERANKER_TOP_K", "2"))
    retry_count = state.get("retry_count", 0)

    # -----------------------------------------------------------------------
    # Case A: 0 retrieved docs
    # -----------------------------------------------------------------------
    if not docs:
        if retry_count < 1:
            # Try reformulating query once
            return {
                "relevant_docs": [],
                "rerank_status": "retry",
                "rerank_score": 0.0,
                "retry_count": retry_count + 1,
            }
        else:
            # Already retried and still 0 docs -> Zone 3 skip
            if state.get("policy_evidence_status") not in {
                "searched",
                "silence_verified",
            }:
                return {
                    "relevant_docs": [],
                    "rerank_status": "skip",
                    "rerank_score": 0.0,
                    "answer": [
                        _make_retrieval_review_verdict(clause_type, clause_text)
                    ],
                }
            return {
                "relevant_docs": [],
                "rerank_status": "skip",
                "rerank_score": 0.0,
                "answer": [_make_skip_verdict(clause_type, clause_text, 0.0, skip_threshold)],
            }

    # -----------------------------------------------------------------------
    # Case B: Rerank documents
    # -----------------------------------------------------------------------
    cohere_key = get_config("COHERE_API_KEY")
    reranked_items: list[dict] = []

    use_cohere = get_config("RERANKER_USE_COHERE", "false").lower() == "true"
    if use_cohere and cohere_key and not cohere_key.startswith("your_"):
        try:
            from langchain_cohere import CohereRerank
            reranker = CohereRerank(
                model="rerank-english-v3.0",
                cohere_api_key=cohere_key,
            )
            # Rerank by clause text (not query)
            rerank_resp = reranker.rerank(query=clause_text, documents=docs)
            for item in rerank_resp:
                idx = item["index"] if isinstance(item, dict) else item.index
                score = item["relevance_score"] if isinstance(item, dict) else item.relevance_score
                reranked_items.append({"index": idx, "score": float(score)})
        except Exception as e:
            print(f"[Warning] Cohere rerank error: {e}. Falling back to RRF scores.")

    # Fallback to RRF score if Cohere is not configured or failed
    score_source = "cohere"
    if not reranked_items:
        score_source = "rrf"
        for idx, doc in enumerate(docs):
            score = float(doc.metadata.get("rrf_score", 0.0))
            reranked_items.append({"index": idx, "score": score})
        reranked_items.sort(key=lambda x: x["score"], reverse=True)

    top_score = reranked_items[0]["score"] if reranked_items else 0.0
    if score_source == "rrf":
        # RRF scores are bounded by the smoothing constant, unlike Cohere's
        # 0..1 relevance scores. Values above .02 are retained for legacy
        # callers that supplied normalized synthetic scores.
        if top_score <= 0.02:
            skip_threshold, retry_threshold = 0.005, 0.012
        else:
            skip_threshold, retry_threshold = 0.15, 0.35

    # -----------------------------------------------------------------------
    # 3-Zone Gate Evaluation
    # -----------------------------------------------------------------------

    # Zone 3: Below skip threshold -> skip LLM entirely
    if top_score < skip_threshold:
        if state.get("policy_evidence_status") not in {
            "searched",
            "silence_verified",
        }:
            return {
                "relevant_docs": [],
                "rerank_status": "skip",
                "rerank_score": top_score,
                "answer": [
                    _make_retrieval_review_verdict(clause_type, clause_text)
                ],
            }
        return {
            "relevant_docs": [],
            "rerank_status": "skip",
            "rerank_score": top_score,
            "answer": [_make_skip_verdict(clause_type, clause_text, top_score, skip_threshold)],
        }

    # Zone 2: Ambiguous (between skip and retry threshold)
    if top_score < retry_threshold and retry_count < 1:
        return {
            "relevant_docs": [],
            "rerank_status": "retry",
            "rerank_score": top_score,
            "retry_count": retry_count + 1,
        }

    # Zone 1: Confident (or already retried Zone 2)
    top_indices = [item["index"] for item in reranked_items[:top_k]]
    relevant_docs: list[Document] = []
    for item in reranked_items[:top_k]:
        doc = docs[item["index"]]
        doc.metadata["relevance_score"] = item["score"]
        doc.metadata["relevance_score_source"] = score_source
        relevant_docs.append(doc)

    return {
        "relevant_docs": relevant_docs,
        "rerank_status": "proceed",
        "rerank_score": top_score,
    }
