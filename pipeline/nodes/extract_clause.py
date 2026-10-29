"""
pipeline/nodes/extract_clause.py — LangGraph node: extract clauses from contract text.

Takes a raw contract text chunk and returns a list of typed Clause objects
using a structured-output LLM call. Hardcoded to 8 clause types for now
(Phase 1 will make this dynamic / fallback-only).
"""

from core.state import AgentState
from core.model import extract_clause_llm


def extract_clauses(state: AgentState) -> AgentState:
    contract_text = state["contract"]
    prompt = f"""Extract the following contract clauses from the input:

- Indemnification
- Warranties and Representations
- Dispute Resolution
- Governing Law and Jurisdiction
- Subcontracting and Assignment
- Intellectual Property
- Limitation of Liability
- Insurance

For each clause:
- Identify and assign a `clause_type` based on its content.
- Provide the clause `text`, limited to one logical unit of meaning per item.
- Do not merge unrelated ideas into the same clause.
- Avoid generic sections, repetitions, and administrative content unless legally relevant.
- Omit headers or section titles unless they contain substantive text.

Return the result as a JSON list of clauses, each with:
- `clause_type`: a short name (use one of the types listed above)
- `text`: one clear, focused clause

Contract:
\"\"\"
{contract_text}
\"\"\"
"""

    for attempt in range(5):
        try:
            extracted = extract_clause_llm.invoke(prompt)
            output = {k: v for k, v in extracted.model_dump().items() if v is not None}
            clauses = [
                clause
                for clause in output.get("clauses", [])
                if clause.get("text", "").strip()
            ]
            unique_clauses = []
            seen_texts = set()
            for clause in clauses:
                normalized_text = " ".join(clause["text"].lower().split())
                if normalized_text not in seen_texts:
                    seen_texts.add(normalized_text)
                    unique_clauses.append(clause)
            return {"extracted_clauses": unique_clauses}
        except Exception as e:
            print(f"[Retry {attempt + 1}/5] Error extracting clauses: {e}")
            if attempt == 4:
                raise RuntimeError(
                    "Failed to extract clauses after multiple retries."
                ) from e
