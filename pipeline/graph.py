"""
pipeline/graph.py — Main LangGraph workflow for contract analysis.

Phase 4 Workflow:
1. START -> extract_clause_sender: Fans out contract chunks to extract_clauses
2. extract_clauses: Extracts typed clauses from contract chunks
3. clause_sender_node: Barrier/aggregation node
4. clause_sender (ReRanker_Sender): Batch embeds all clauses (1 call) and fans out to SubGraph
5. SubGraph: Runs per-clause compliance check against retrieved policy chunks (with 2-tier cache & citation verification)
6. missing_clause_checker: Reverse retrieval identifying mandatory company policy rules omitted by the vendor
7. missing_clause_checker -> END: Final report contains both present clause verdicts and missing clause alerts!
"""

from langgraph.graph import StateGraph, END, START

from core.state import AgentState
from pipeline.subgraph import sub_graph
from pipeline.nodes.extract_clause import extract_clauses
from pipeline.nodes.extract_clause_sender import extract_clause_sender
from pipeline.nodes.clause_sender_node import clause_sender_node
from pipeline.nodes.clause_sender import ReRanker_Sender
from pipeline.nodes.missing_clause_checker import missing_clause_checker

graph = StateGraph(AgentState)

graph.add_node("extract_clauses", extract_clauses)
graph.add_node("clause_sender_node", clause_sender_node)
graph.add_node("SubGraph", sub_graph)
graph.add_node("missing_clause_checker", missing_clause_checker)

graph.add_conditional_edges(
    START,
    extract_clause_sender,
    {
        "extract_clauses": "extract_clauses",
    },
)

graph.add_edge("extract_clauses", "clause_sender_node")

graph.add_conditional_edges(
    "clause_sender_node",
    ReRanker_Sender,
    {
        "SubGraph": "SubGraph",
    },
)

# After all SubGraph evaluations finish, run reverse retrieval for missing clauses
graph.add_edge("SubGraph", "missing_clause_checker")
graph.add_edge("missing_clause_checker", END)

app = graph.compile()
