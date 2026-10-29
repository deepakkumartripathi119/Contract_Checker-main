"""
core/state.py — All shared Pydantic schemas and LangGraph TypedDict state definitions.

Nothing in this file imports from pipeline/ — it is the lowest-level module
and can be safely imported by any other package.
"""

from typing import TypedDict, Annotated, List, Optional
from operator import add
from pydantic import BaseModel, Field
from langchain.schema import Document


# ---------------------------------------------------------------------------
# Pydantic schemas (structured LLM output)
# ---------------------------------------------------------------------------

class Clause(BaseModel):
    text: Optional[str] = Field(default=None, description="The raw clause text")
    clause_type: Optional[str] = Field(default=None, description="The type of clause")


class ExtractedClauses(BaseModel):
    clauses: List[Clause]


class ClauseComplianceResult(BaseModel):
    clause_text: str = Field(..., description="The original clause text being evaluated.")
    policy_source: str = Field(
        ...,
        description="The source or identifier of the internal policy used for comparison.",
    )
    reason: str = Field(
        ...,
        description="Explanation of the compliance decision. Keep short and to the point.",
    )
    compliant: bool = Field(
        ..., description="Whether the clause is compliant with internal policy."
    )
    clauses_internal: List[str] = Field(
        ...,
        description=(
            "List of clauses from internal policies, each formatted as: "
            "'clause text — `source`'."
        ),
    )
    suggested_revision: Optional[str] = Field(
        default=None,
        description="Suggested revision if non-compliant. Keep short and to the point.",
    )
    exact_policy_quote: Optional[str] = Field(
        default=None,
        description="Exact verbatim quote from policy document used as evidence.",
    )
    citation_verified: Optional[bool] = Field(
        default=None,
        description="Whether exact_policy_quote was deterministically verified in retrieved policy docs.",
    )
    policy_evidence_status: Optional[str] = Field(
        default=None,
        description=(
            "Evidence state: topic_found, silence_verified, or retrieval_failed."
        ),
    )
    policy_requirement_id: Optional[str] = Field(
        default=None,
        description="Stable identifier for a missing policy requirement.",
    )
    is_missing_clause: Optional[bool] = Field(
        default=False,
        description="Whether this result represents an omitted mandatory policy clause.",
    )


class MicroBatchComplianceResult(BaseModel):
    results: List[ClauseComplianceResult] = Field(
        description="List of compliance verdicts for all clauses evaluated in the micro-batch."
    )


class MicroBatchLLMItem(BaseModel):
    clause_id: Optional[int] = None
    status: Optional[str] = None
    clause_text: Optional[str] = None
    policy_source: Optional[str] = None
    reason: str
    clauses_internal: List[str] = Field(default_factory=list)
    compliant: Optional[bool] = None
    suggested_revision: Optional[str] = None
    exact_policy_quote: Optional[str] = None


class MicroBatchLLMResult(BaseModel):
    results: List[MicroBatchLLMItem]


class query(BaseModel):
    query: str = Field(
        description="A generated query for retrieval from vectorstore "
                    "according to the clause type and clause text."
    )


# ---------------------------------------------------------------------------
# LangGraph state reducers
# ---------------------------------------------------------------------------

def _retrieved_docs_reducer(a: List[Document], b: List[Document]) -> List[Document]:
    return a + b


def _missing_clauses_reducer(
    existing: List[dict],
    incoming: List[dict],
) -> List[dict]:
    """Merge missing findings idempotently across graph branches."""
    merged: dict[str, dict] = {}
    for finding in [*existing, *incoming]:
        requirement_id = finding.get("policy_requirement_id")
        if not requirement_id:
            requirement_id = " ".join(
                [
                    str(finding.get("clause_title", "")).lower(),
                    str(finding.get("exact_policy_quote", "")).lower(),
                ]
            ).split()
            requirement_id = " ".join(requirement_id)
        merged[str(requirement_id)] = finding
    return list(merged.values())


# ---------------------------------------------------------------------------
# LangGraph state TypedDicts
# ---------------------------------------------------------------------------

class AgentState(TypedDict, total=False):
    org_id: str
    contract: Annotated[List[str], add]
    extracted_clauses: Annotated[List[Clause], add]
    answer: Annotated[List[ClauseComplianceResult], add]
    missing_clauses: Annotated[List[dict], _missing_clauses_reducer]
    compliance_batch: list[dict]


class SubState(TypedDict, total=False):
    org_id: str
    clause: dict
    clause_embedding: Optional[list[float]]
    query: str
    retrived_docs: Annotated[List[Document], _retrieved_docs_reducer]
    relevant_docs: Annotated[List[Document], _retrieved_docs_reducer]
    answer: Annotated[List[ClauseComplianceResult], add]
    rerank_status: str       # "proceed" | "retry" | "skip"
    rerank_score: float      # top rerank confidence score
    retry_count: int         # number of retry attempts
    policy_evidence_status: str  # "topic_found", "silence_verified", or "retrieval_failed"
