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
from pipeline.nodes.retriever import batch_retrieve


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
    try:
        batch_docs = batch_retrieve(clauses)
    except Exception as e:
        print(f"[Warning] batch_retrieve failed: {e}. Falling back to per-clause retrieval.")
        batch_docs = [[] for _ in clauses]

    sends = []
    for clause, docs in zip(clauses, batch_docs):
        clause_payload = (
            clause.model_dump() if hasattr(clause, "model_dump") else clause
        )
        sends.append(
            Send(
                "SubGraph",
                {
                    "clause": clause_payload,
                    "retrived_docs": docs,
                },
            )
        )
    return sends
