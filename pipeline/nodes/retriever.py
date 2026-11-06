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

from langchain.schema import Document
from langsmith import traceable

from core.model import get_embedding_model
from core.settings import get_config, get_default_org_id
from core.state import SubState, Clause
from core.vector_store import Chunk, get_vector_store
from core.retrieval import (
    deduplicate_parents_max_pooling,
    policy_topic_matches,
    rrf_merge,
)
from core.cache import get_cache, policy_set_hash
from pipeline.nodes.document_parser import _doc_id, parse_policy_pdf


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
    incoming = []
    for file in uploaded_files:
        pdf_bytes = file.read() if hasattr(file, "read") else file
        incoming.append((getattr(file, "name", "unknown.pdf"), pdf_bytes))
    digest = policy_set_hash([data for _, data in incoming])
    cache = get_cache()
    if digest and cache.get_policy_hash(org_id) == digest:
        _rename_indexed_policy(store, org_id, incoming)
        return _stored_doc_ids(store, org_id)

    embedding_model = get_embedding_model()

    all_chunks: list[Chunk] = []
    child_chunks: list[Chunk] = []
    doc_ids: list[str] = []

    for doc_name, pdf_bytes in incoming:
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

    # The new index and the policy version commit together. A crash rolls both back.
    previous_chunks = store.export_org_chunks(org_id)

    def _publish(connection) -> None:
        from core.tenant import advance_policy_version

        try:
            advance_policy_version(connection, org_id)
        except Exception as exc:
            raise RuntimeError("Policy version was not saved.") from exc

    try:
        store.replace_org_chunks(org_id, all_chunks, alongside=_publish)
    except Exception:
        try:
            store.replace_org_chunks(org_id, previous_chunks)
        except Exception as restore_error:
            raise RuntimeError(
                "Policy activation failed and the previous policy index could not be restored."
            ) from restore_error
        raise
    try:
        from core.tenant import get_tenant_config

        get_tenant_config(org_id)
    except Exception:
        pass

    cache.invalidate(org_id)
    cache.set_policy_hash(org_id, digest)

    return doc_ids


def _stored_doc_ids(store, org_id: str) -> list[str]:
    seen = []
    for chunk in store.export_org_chunks(org_id):
        if chunk.doc_id and chunk.doc_id not in seen:
            seen.append(chunk.doc_id)
    return seen


def _rename_indexed_policy(store, org_id: str, incoming: list[tuple[str, bytes]]) -> None:
    """Point the indexed sections at the new file name. The vectors stay."""
    names = {_doc_id(data): name for name, data in incoming}
    chunks = store.export_org_chunks(org_id)
    changed = False
    for chunk in chunks:
        new_name = names.get(chunk.doc_id)
        old_name = (chunk.metadata or {}).get("doc_name")
        if not new_name or not old_name or new_name == old_name:
            continue
        chunk.metadata["doc_name"] = new_name
        chunk.breadcrumb = chunk.breadcrumb.replace(f"Doc: {old_name}", f"Doc: {new_name}", 1)
        changed = True
    if changed:
        store.replace_org_chunks(org_id, chunks)


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
        parent_docs = deduplicate_parents_max_pooling(scored_chunks, store, top_k=top_k, org_id=org_id)
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
    docs = deduplicate_parents_max_pooling(scored_chunks, store, top_k=top_k, org_id=org_id)

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
