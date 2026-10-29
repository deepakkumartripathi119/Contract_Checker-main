"""
pipeline/nodes/clause_sender.py — Fan-out: batch retrieval and SubGraph dispatch.

Sends ALL extracted clauses to the SubGraph for compliance checking.
Phase 2 Modernization:
- Runs batch_retrieve() for all extracted clauses in ONE single embedding call.
- Attaches the pre-retrieved policy documents directly into each SubGraph state,
  killing the N+1 sequential embedding and LLM query generation bottleneck.
- Concurrency is capped by max_concurrency in app.py to avoid rate limits.
"""

from langgraph.types import Send
from core.state import AgentState
from pipeline.nodes.retriever import batch_retrieve, document_retriever
from core.vector_store import get_vector_store
from core.model import get_default_org_id
from pipeline.nodes.retriever import policy_topic_matches
from pipeline.nodes.query_generator import generate_relevant_docs_node
from pipeline.nodes.reranker import Reranker


def ReRanker_Sender(state: AgentState) -> list:
    """Fan-out ALL extracted clauses to the SubGraph with pre-retrieved documents.

    1. Gathers all extracted clauses.
    2. Executes batch_retrieve() to embed all clauses in 1 call and perform
       hybrid dense + BM25 RRF retrieval.
    3. Emits Send("SubGraph", {"clause": clause, "retrived_docs": docs}) for each clause.
    """
    clauses = state.get("extracted_clauses", [])
    if not clauses:
        return []

    # Run batch retrieval across all clauses in a single batch network call
    retrieval_failed = False
    try:
        current_org = state.get("org_id", get_default_org_id())
        batch_docs, embeddings = batch_retrieve(
            clauses,
            org_id=current_org,
            return_embeddings=True,
        )
    except Exception as e:
        print(f"[Warning] batch_retrieve failed: {e}. Falling back to per-clause retrieval.")
        retrieval_failed = True
        batch_docs = [[] for _ in clauses]
        embeddings = [[] for _ in clauses]

    sends = []
    proceed_batch = []
    policy_sections = get_vector_store().get_policy_sections(current_org)
    for clause, docs, embedding in zip(clauses, batch_docs, embeddings):
        clause_payload = (
            clause.model_dump() if hasattr(clause, "model_dump") else clause
        )
        clause_type = clause_payload.get("clause_type", "Unknown")
        topic_exists = any(
            policy_topic_matches(
                clause_type,
                section.metadata.get("heading", ""),
                section.text,
            )
            for section in policy_sections
        )
        policy_status = (
            "retrieval_failed"
            if retrieval_failed or not policy_sections
            else "topic_found" if topic_exists else "silence_verified"
        )
        substate = {
            "org_id": current_org,
            "clause": clause_payload,
            "retrived_docs": docs,
            "clause_embedding": embedding,
            "retry_count": 0,
            "policy_evidence_status": policy_status,
        }
        
        # 1. Initial Rerank
        rerank_result = Reranker(substate)
        substate.update(rerank_result)
        
        # 2. Zone 2 Retry Loop (Inline)
        if substate.get("rerank_status") == "retry":
            # Generate new query
            query_update = generate_relevant_docs_node(substate)
            substate.update(query_update)
            # Clear old docs so retriever performs a fresh search
            substate.pop("retrived_docs", None)
            # Re-retrieve
            docs_update = document_retriever(substate)
            substate.update(docs_update)
            # Rerank again
            rerank_result = Reranker(substate)
            substate.update(rerank_result)
            
        # 3. Route based on final status
        if substate.get("rerank_status") == "proceed":
            proceed_batch.append(substate)
        elif substate.get("rerank_status") == "skip":
            # Send skipped clauses to a passthrough node to append their verdict
            sends.append(Send("SkipNode", substate))

    for start in range(0, len(proceed_batch), 5):
        sends.append(
            Send(
                "BulkCompliance",
                {"compliance_batch": proceed_batch[start:start + 5]},
            )
        )
    return sends
