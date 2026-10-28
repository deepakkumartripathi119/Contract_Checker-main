"""
pipeline/subgraph.py — Per-clause compliance evaluation subgraph.

Phase 3 Modernized Flow with 3-Zone Gate
----------------------------------------
1. document_retriever: Fast direct hybrid retrieval (Dense + BM25 RRF + parent max-pooling).
   If batch_retrieve() already populated 'retrived_docs', this is an instant no-op passthrough!
2. Reranker (3-Zone Gate):
   - Zone 3 (score < 0.15) -> "skip": deterministic verdict generated immediately, routes to END! (0 LLM calls)
   - Zone 2 (0.15 <= score < 0.35) -> "retry": routes to query_generator -> retries document_retriever -> Reranker
   - Zone 1 (score >= 0.35) -> "proceed": routes to compliance_checker -> END!
3. query_generator: Fallback LLM query generation when reranker confidence is ambiguous.
4. compliance_checker: Evaluates clause against reranked policy documents.
"""

from langgraph.graph import StateGraph, END, START

from core.state import SubState
from pipeline.nodes.query_generator import generate_relevant_docs_node
from pipeline.nodes.retriever import document_retriever
from pipeline.nodes.reranker import Reranker
from pipeline.nodes.compliance_checker import compliance_checker


def _route_after_reranker(state: SubState) -> str:
    """
    3-Zone Confidence Gate router:
    - "skip"   : Irrelevant clause (Zone 3). Answer is already set; route to END.
    - "retry"  : Ambiguous match (Zone 2). Route to query_generator to reformulate.
    - "proceed": Confident match (Zone 1). Route to compliance_checker.
    """
    status = state.get("rerank_status", "proceed")
    if status == "retry":
        return "query_generator"
    elif status == "skip":
        return END
    return "compliance_checker"


graph_sub = StateGraph(SubState)

graph_sub.add_node("document_retriever", document_retriever)
graph_sub.add_node("Reranker", Reranker)
graph_sub.add_node("query_generator", generate_relevant_docs_node)
graph_sub.add_node("compliance_checker", compliance_checker)

# 1. Start directly with retrieval
graph_sub.add_edge(START, "document_retriever")

# 2. Retrieved docs go directly to Reranker for 3-zone confidence scoring
graph_sub.add_edge("document_retriever", "Reranker")

# 3. 3-Zone Gate conditional routing from Reranker
graph_sub.add_conditional_edges(
    "Reranker",
    _route_after_reranker,
    {
        "query_generator": "query_generator",
        "compliance_checker": "compliance_checker",
        END: END,
    },
)

# 4. If retried via query_generator, re-run retrieval then rerank again
graph_sub.add_edge("query_generator", "document_retriever")

# 5. Compliant evaluation finishes at END
graph_sub.add_edge("compliance_checker", END)

sub_graph = graph_sub.compile()
