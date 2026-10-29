"""LangGraph node for fallback contract clause extraction."""

from core.state import AgentState
from core.model import extract_clause_llm, get_default_org_id
from core.vector_store import get_vector_store
from pipeline.nodes.document_parser import remove_synthetic_expected_annotations
from prompts.clause_extraction import CLAUSE_EXTRACTION_SYSTEM_PROMPT


def extract_clauses(state: AgentState) -> dict:
    contract_text = remove_synthetic_expected_annotations(
        state.get("contract", [""])[0]
    )
    org_id = state.get("org_id", get_default_org_id())

    store = get_vector_store()
    policy_sections = store.get_policy_sections(org_id)

    allowed_types = {
        f"- {title}"
        for sec in policy_sections
        if (title := sec.metadata.get("heading", ""))
        and title.lower() not in (
            "flat",
            "unknown",
            "section",
            "internal policy document",
        )
    }

    allowed_types_str = "\n".join(sorted(allowed_types)) or (
        "(No policy headings were available. Derive each clause type from the "
        "contract's own heading or substantive topic.)"
    )
    system_instruction = CLAUSE_EXTRACTION_SYSTEM_PROMPT.format(
        allowed_clause_types=allowed_types_str
    )
    prompt = f'{system_instruction}\n\nContract:\n"""\n{contract_text}\n"""'

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
