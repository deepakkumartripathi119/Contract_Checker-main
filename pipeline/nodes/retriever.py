"""
pipeline/nodes/retriever.py — Policy ingestion, hybrid retrieval, and batch embedding.

Phase 2 Modernizations
----------------------
1. Batch retrieval: batch_retrieve() embeds all extracted clauses in ONE single
   API call, eliminating the N+1 sequential embedding bottleneck.
2. Weighted Reciprocal Rank Fusion (RRF): rrf_merge() fuses dense vector
   similarity and BM25 sparse keyword rankings with tunable weights:
     RRF_score(d) = w_dense / (k + rank_dense) + w_sparse / (k + rank_sparse)
3. Parent deduplication with max-pooling: deduplicate_parents_max_pooling()
   groups retrieved child sub-clauses by their parent section ID, assigning
   the parent the maximum RRF score of its matching children. The LLM receives
   the complete section context once, without duplicate chunks.
4. Pass-through optimization: document_retriever() checks if 'retrived_docs'
   were already populated by batch_retrieve() in the parent graph, bypassing
   redundant re-retrieval.
"""

from __future__ import annotations

import re
import os
from typing import Any, Optional
from langchain.schema import Document
from langsmith import traceable

from core.model import get_embedding_model, get_config, get_default_org_id
from core.state import SubState, Clause
from core.vector_store import Chunk, VectorStoreBackend, get_vector_store
from pipeline.nodes.document_parser import parse_policy_pdf


def policy_topic_matches(clause_type: str, heading: str, text: str) -> bool:
    """Check topic overlap without assuming a fixed policy taxonomy."""
    ignored = {
        "a", "an", "and", "clause", "contract", "for", "in", "of", "on",
        "policy", "provision", "section", "term", "terms", "the", "to",
    }

    def tokens(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"\b[a-z]{3,}\b", value.lower())
            if token not in ignored
        }

    clause_terms = tokens(clause_type)
    searchable = tokens(f"{heading} {text}")
    return bool(clause_terms and clause_terms.intersection(searchable))


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion (RRF) & Parent Deduplication with Max-Pooling
# ---------------------------------------------------------------------------

@traceable
def rrf_merge(
    dense_hits: list[tuple[float, Chunk]] | list[Chunk],
    sparse_hits: list[tuple[float, Chunk]] | list[Chunk],
    w_dense: float = 0.7,
    w_sparse: float = 0.3,
    k: int = 60,
) -> list[tuple[Chunk, float]]:
    """
    Combine dense and sparse search rankings using Reciprocal Rank Fusion (RRF).

    Formula:
        RRF_score(d) = w_dense * (1 / (k + rank_dense(d))) + w_sparse * (1 / (k + rank_sparse(d)))

    Parameters
    ----------
    dense_hits : ranked results from dense search (highest score first)
    sparse_hits: ranked results from sparse BM25 search (highest score first)
    w_dense    : weight for dense retrieval (default 0.7)
    w_sparse   : weight for sparse retrieval (default 0.3)
    k          : smoothing constant (default 60, per Cormack et al.)

    Returns
    -------
    List of (Chunk, combined_rrf_score) sorted descending by combined score.
    """
    chunk_map: dict[str, Chunk] = {}
    rrf_scores: dict[str, float] = {}

    def _extract_chunk(item: Any) -> Chunk:
        return item[1] if isinstance(item, tuple) else item

    for rank, item in enumerate(dense_hits, start=1):
        chunk = _extract_chunk(item)
        cid = chunk.id
        chunk_map[cid] = chunk
        rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (w_dense / (k + rank))

    for rank, item in enumerate(sparse_hits, start=1):
        chunk = _extract_chunk(item)
        cid = chunk.id
        chunk_map[cid] = chunk
        rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (w_sparse / (k + rank))

    sorted_results = [
        (chunk_map[cid], rrf_scores[cid])
        for cid in sorted(rrf_scores.keys(), key=lambda cid: rrf_scores[cid], reverse=True)
    ]
    return sorted_results


@traceable
def deduplicate_parents_max_pooling(
    scored_chunks: list[tuple[Chunk, float]],
    store: VectorStoreBackend,
    top_k: int = 5,
) -> list[Document]:
    """
    Group matched child chunks by parent_id and max-pool their RRF scores.

    Assigns the parent chunk the maximum score among all its matching children.
    Fetches the full parent section text to provide rich context for the LLM.
    For flat documents where parent_id is None, the child chunk is used directly.
    """
    if not scored_chunks:
        return []

    parent_scores: dict[str, float] = {}
    parent_matched_children: dict[str, list[dict]] = {}
    fallback_chunk_map: dict[str, Chunk] = {}

    for child, score in scored_chunks:
        target_id = child.parent_id or child.id
        fallback_chunk_map[child.id] = child

        if target_id not in parent_scores or score > parent_scores[target_id]:
            parent_scores[target_id] = score

        if target_id not in parent_matched_children:
            parent_matched_children[target_id] = []
        parent_matched_children[target_id].append({
            "child_id": child.id,
            "breadcrumb": child.breadcrumb,
            "score": score,
        })

    sorted_parent_ids = sorted(
        parent_scores.keys(),
        key=lambda pid: parent_scores[pid],
        reverse=True,
    )[:top_k]

    docs: list[Document] = []
    for pid in sorted_parent_ids:
        parent_chunk = store.get_parent(pid)
        if not parent_chunk:
            parent_chunk = fallback_chunk_map.get(pid)

        if not parent_chunk:
            continue

        doc = Document(
            page_content=parent_chunk.text,
            metadata={
                **parent_chunk.metadata,
                "breadcrumb": parent_chunk.breadcrumb,
                "source": parent_chunk.metadata.get("doc_name", "policy"),
                "chunk_id": parent_chunk.id,
                "rrf_score": parent_scores[pid],
                "matched_children": parent_matched_children.get(pid, []),
            },
        )
        docs.append(doc)

    return docs


# ---------------------------------------------------------------------------
# Policy ingestion
# ---------------------------------------------------------------------------

@traceable
def process_uploaded_files(
    uploaded_files,
    org_id: str | None = None,
) -> list[str]:
    """
    Ingest a list of uploaded policy PDF files for an organisation.
    """
    org_id = org_id or get_default_org_id()
    store = get_vector_store()
    embedding_model = get_embedding_model()

    all_chunks: list[Chunk] = []
    child_chunks: list[Chunk] = []
    doc_ids: list[str] = []

    for file in uploaded_files:
        pdf_bytes = file.read() if hasattr(file, "read") else file
        doc_name = getattr(file, "name", "unknown.pdf")

        chunks = parse_policy_pdf(pdf_bytes, doc_name, org_id=org_id)
        all_chunks.extend(chunks)

        file_children = [c for c in chunks if c.chunk_type == "child"]
        child_chunks.extend(file_children)

        if chunks:
            doc_ids.append(chunks[0].doc_id)

    if not child_chunks:
        return doc_ids

    # Batch embed all child chunks in a single call (staged validation)
    texts = [c.text for c in child_chunks]
    embeddings = embedding_model.embed_documents(texts)

    for chunk, emb in zip(child_chunks, embeddings):
        chunk.embedding = emb

    # Atomic staged swap: replace existing chunks only after parsing and embedding succeed
    store.replace_org_chunks(org_id, all_chunks)

    # Invalidate cache and bump policy version for this org
    try:
        from core.tenant import bump_tenant_policy_version
        bump_tenant_policy_version(org_id)
    except Exception:
        pass
    from core.cache import get_cache
    get_cache().invalidate(org_id)

    return doc_ids


# ---------------------------------------------------------------------------
# Batch Retrieval for All Clauses (Kills N+1 LLM & Embedding calls)
# ---------------------------------------------------------------------------

@traceable
def batch_retrieve(
    clauses: list[dict | Clause],
    org_id: str | None = None,
    top_k: int | None = None,
    return_embeddings: bool = False,
) -> list[list[Document]] | tuple[list[list[Document]], list[list[float]]]:
    """
    Retrieve relevant policy documents for ALL clauses in ONE batched embedding call.

    Parameters
    ----------
    clauses: list of dicts or Clause objects (each with 'text' and optional 'clause_type')
    org_id : organisation ID (falls back to POLICY_ORG_ID env var)
    top_k  : number of parent documents to return per clause

    Returns
    -------
    List of Document lists, exactly aligned with the input clauses:
      [ [doc1, doc2, ...], [doc1, doc2, ...], ... ]
    """
    if not clauses:
        return []

    org_id = org_id or get_default_org_id()
    top_k = top_k or int(get_config("RETRIEVAL_TOP_K", "5"))
    w_dense = float(get_config("RRF_WEIGHT_DENSE", "0.7"))
    w_sparse = float(get_config("RRF_WEIGHT_SPARSE", "0.3"))
    rrf_k = int(get_config("RRF_K", "60"))

    store = get_vector_store()
    embedding_model = get_embedding_model()

    clause_texts = []
    for c in clauses:
        if hasattr(c, "text"):
            clause_texts.append(c.text or "")
        elif isinstance(c, dict):
            clause_texts.append(c.get("text") or "")
        else:
            clause_texts.append(str(c))

    # Single batched embedding network call for all clauses
    all_embeddings = embedding_model.embed_documents(clause_texts)

    results: list[list[Document]] = []
    for text, emb in zip(clause_texts, all_embeddings):
        if not text.strip():
            results.append([])
            continue

        dense_hits = store.search_dense(org_id, emb, top_k=top_k * 2)
        sparse_hits = store.search_sparse(org_id, text, top_k=top_k * 2)
        scored_chunks = rrf_merge(
            dense_hits, sparse_hits, w_dense=w_dense, w_sparse=w_sparse, k=rrf_k
        )
        parent_docs = deduplicate_parents_max_pooling(scored_chunks, store, top_k=top_k)
        results.append(parent_docs)

    if return_embeddings:
        return results, all_embeddings
    return results


# ---------------------------------------------------------------------------
# Per-clause retrieval (LangGraph node)
# ---------------------------------------------------------------------------

def document_retriever(state: SubState) -> SubState:
    """
    LangGraph node: retrieve relevant policy chunks for a single vendor clause.

    Execution path:
    1. If state['retrived_docs'] is already populated (via batch_retrieve),
       bypass retrieval immediately.
    2. Otherwise (e.g. manual single-clause input or retry after query reformulation),
       run hybrid search (dense + BM25 + Weighted RRF + parent max-pooling).
    """
    # 1. Fast path: already batch-retrieved
    existing_docs = state.get("retrived_docs", [])
    if existing_docs:
        return {"retrived_docs": existing_docs}

    clause_data = state.get("clause", {})
    clause_text = (
        clause_data.get("text", "")
        if isinstance(clause_data, dict)
        else getattr(clause_data, "text", "")
    )
    query_text = state.get("query") or clause_text

    if not query_text or not query_text.strip():
        return {"retrived_docs": []}

    org_id = state.get("org_id", get_default_org_id())
    top_k = int(get_config("RETRIEVAL_TOP_K", "5"))
    w_dense = float(get_config("RRF_WEIGHT_DENSE", "0.7"))
    w_sparse = float(get_config("RRF_WEIGHT_SPARSE", "0.3"))
    rrf_k = int(get_config("RRF_K", "60"))

    store = get_vector_store()
    embedding_model = get_embedding_model()

    query_embedding = embedding_model.embed_query(query_text)
    dense_hits = store.search_dense(org_id, query_embedding, top_k=top_k * 2)
    sparse_hits = store.search_sparse(org_id, query_text, top_k=top_k * 2)

    scored_chunks = rrf_merge(
        dense_hits, sparse_hits, w_dense=w_dense, w_sparse=w_sparse, k=rrf_k
    )
    docs = deduplicate_parents_max_pooling(scored_chunks, store, top_k=top_k)

    policy_sections = store.get_policy_sections(org_id)
    clause_type = (
        clause_data.get("clause_type", "")
        if isinstance(clause_data, dict)
        else getattr(clause_data, "clause_type", "")
    )
    topic_found = any(
        policy_topic_matches(
            clause_type,
            section.metadata.get("heading", ""),
            section.text,
        )
        for section in policy_sections
    )
    return {
        "retrived_docs": docs,
        "policy_evidence_status": (
            "retrieval_failed"
            if not policy_sections
            else "topic_found" if topic_found else "silence_verified"
        ),
    }
