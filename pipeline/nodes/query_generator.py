"""
pipeline/nodes/query_generator.py — LangGraph node: conditional query reformulation.

Phase 2 Role
------------
Acts as a fallback query reformulation node. Only called when direct hybrid
retrieval (using the clause text directly) returns 0 policy documents.

Uses the LLM to produce a focused search query from a clause's text and type,
then loops back to document_retriever for a second search attempt.
"""

from core.model import query_generator_llm
from core.state import SubState
from prompts.query_generator import QUERY_GENERATOR_SYSTEM_PROMPT


def generate_relevant_docs_node(state: SubState) -> dict:
    """
    Generate a focused internal search query using the LLM when direct search yields 0 docs.
    """
    clause_data = state.get("clause", {})
    clause_text = (
        clause_data.get("text", "")
        if isinstance(clause_data, dict)
        else getattr(clause_data, "text", "")
    )
    clause_type = (
        clause_data.get("clause_type", "general")
        if isinstance(clause_data, dict)
        else getattr(clause_data, "clause_type", "general")
    )

    prompt = f"""{QUERY_GENERATOR_SYSTEM_PROMPT}

Clause Type: {clause_type}

Clause Text:
\"\"\"
{clause_text}
\"\"\"
"""

    for attempt in range(3):
        try:
            response = query_generator_llm.invoke(prompt)
            query_obj = response.model_dump()
            return {"query": query_obj["query"]}
        except Exception as e:
            print(f"[Retry {attempt + 1}/3] Error generating query: {e}")
            if attempt == 2:
                # Graceful fallback: return clause text as query rather than crashing
                return {"query": clause_text}

    return {"query": clause_text}
