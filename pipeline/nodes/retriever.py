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

import os
from typing import Any, Optional
from langchain.schema import Document

from core.model import get_embedding_model, get_config
from core.state import SubState, Clause
from core.vector_store import Chunk, VectorStoreBackend, get_vector_store
from pipeline.nodes.document_parser import parse_policy_pdf


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion (RRF) & Parent Deduplication with Max-Pooling
# ---------------------------------------------------------------------------

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

def process_uploaded_files(
    uploaded_files,
    org_id: str | None = None,
) -> list[str]:
    """
    Ingest a list of uploaded policy PDF files for an organisation.

    1. Parse each PDF with document_parser → list[Chunk] (parent + child tree)
    2. Collect all child chunks across all files
    3. Embed all child chunks in ONE batch call
    4. Persist all chunks to the vector store
    """
    org_id = org_id or get_config("POLICY_ORG_ID", "nexify")
    store = get_vector_store()
    
    # WIPE ghost data before ingesting new policies to prevent duplicates
    store.clear_org(org_id)

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

    # Batch embed all child chunks in a single call
    texts = [c.text for c in child_chunks]
    embeddings = embedding_model.embed_documents(texts)

    for chunk, emb in zip(child_chunks, embeddings):
        chunk.embedding = emb

    store.add_chunks(org_id, all_chunks)

    # Invalidate cache for this org when policy changes (Phase 4)
    from core.cache import get_cache
    get_cache().invalidate(org_id)

    return doc_ids


# ---------------------------------------------------------------------------
# Batch Retrieval for All Clauses (Kills N+1 LLM & Embedding calls)
# ---------------------------------------------------------------------------

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

    org_id = org_id or get_config("POLICY_ORG_ID", "nexify")
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

    org_id = get_config("POLICY_ORG_ID", "nexify")
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

    return {"retrived_docs": docs}
