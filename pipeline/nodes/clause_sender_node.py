"""
pipeline/nodes/clause_sender_node.py — Empty passthrough node.

Required by the graph structure as a synchronisation point between
extract_clauses (parallel fan-in) and clause_sender (fan-out to SubGraph).
Returns an empty dict — LangGraph merges accumulated state automatically.
"""

from core.state import AgentState


def clause_sender_node(state: AgentState) -> dict:
    return {}

