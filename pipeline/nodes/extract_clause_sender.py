"""
pipeline/nodes/extract_clause_sender.py — Fan-out: one Send per contract chunk.

Sends each 4-page text chunk to the extract_clauses node in parallel.
Phase 1 will replace the upstream chunking logic (in app.py) with
layout-aware Markdown parsing, making this node receive heading-split chunks.
"""

from langgraph.types import Send
from core.state import AgentState


def extract_clause_sender(state: AgentState) -> list:
    """Return a Send for each contract chunk → parallel extraction."""
    return [
        Send("extract_clauses", {"contract": contract})
        for contract in state["contract"]
    ]

