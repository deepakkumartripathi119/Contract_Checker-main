"""Retrieval rules shared by search and compliance.

These functions decide how rankings are combined and whether a clause and a
policy section are about the same topic. They do not open a database and they
do not call a model. The pipeline nodes call them; a future search backend
can call the same functions.
"""

from __future__ import annotations

import re
from typing import Any

from langchain.schema import Document
from langsmith import traceable

from core.vector_store import Chunk, VectorStoreBackend


def policy_topic_matches(clause_type: str, heading: str, text: str) -> bool:
    """Check topic overlap without assuming a fixed legal taxonomy."""
    ignored = {
        "a", "an", "and", "clause", "contract", "for", "in", "of", "on",
        "policy", "provision", "section", "term", "terms", "the", "to",
    }

    def tokens(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"\b\w{2,}\b", value.lower(), re.UNICODE)
            if token not in ignored
        }

    clause_terms = tokens(clause_type)
    searchable = tokens(f"{heading} {text}")
    if clause_terms and clause_terms.intersection(searchable):
        return True

    return False


@traceable
def rrf_merge(
    dense_hits: list[tuple[float, Chunk]] | list[Chunk],
    sparse_hits: list[tuple[float, Chunk]] | list[Chunk],
    w_dense: float = 0.7,
    w_sparse: float = 0.3,
    k: int = 60,
) -> list[tuple[Chunk, float]]:
    """Combine dense and sparse rankings with reciprocal rank fusion."""
    chunk_map: dict[str, Chunk] = {}
    rrf_scores: dict[str, float] = {}

    def _extract_chunk(item: Any) -> Chunk:
        return item[1] if isinstance(item, tuple) else item

    for rank, item in enumerate(dense_hits, start=1):
        chunk = _extract_chunk(item)
        cid = chunk.id
        chunk_map[cid] = chunk
        rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (w_dense / (k + rank))

    for rank, item in enumerate(sparse_hits, start=1):
        chunk = _extract_chunk(item)
        cid = chunk.id
        chunk_map[cid] = chunk
        rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (w_sparse / (k + rank))

    return [
        (chunk_map[cid], rrf_scores[cid])
        for cid in sorted(rrf_scores.keys(), key=lambda cid: rrf_scores[cid], reverse=True)
    ]


@traceable
def deduplicate_parents_max_pooling(
    scored_chunks: list[tuple[Chunk, float]],
    store: VectorStoreBackend,
    top_k: int = 5,
    org_id: str | None = None,
) -> list[Document]:
    """Group child hits by parent and keep the strongest score for each parent."""
    if not scored_chunks:
        return []

    parent_scores: dict[str, float] = {}
    parent_matched_children: dict[str, list[dict]] = {}
    fallback_chunk_map: dict[str, Chunk] = {}

    for child, score in scored_chunks:
        target_id = child.parent_id or child.id
        fallback_chunk_map[child.id] = child

        if target_id not in parent_scores or score > parent_scores[target_id]:
            parent_scores[target_id] = score

        if target_id not in parent_matched_children:
            parent_matched_children[target_id] = []
        parent_matched_children[target_id].append({
            "child_id": child.id,
            "breadcrumb": child.breadcrumb,
            "score": score,
        })

    sorted_parent_ids = sorted(
        parent_scores.keys(),
        key=lambda pid: parent_scores[pid],
        reverse=True,
    )[:top_k]

    docs: list[Document] = []
    for pid in sorted_parent_ids:
        parent_chunk = store.get_parent(pid, org_id=org_id)
        if not parent_chunk:
            parent_chunk = fallback_chunk_map.get(pid)

        if not parent_chunk:
            continue

        docs.append(
            Document(
                page_content=parent_chunk.text,
                metadata={
                    **parent_chunk.metadata,
                    "breadcrumb": parent_chunk.breadcrumb,
                    "source": parent_chunk.metadata.get("doc_name", "policy"),
                    "chunk_id": parent_chunk.id,
                    "rrf_score": parent_scores[pid],
                    "matched_children": parent_matched_children.get(pid, []),
                },
            )
        )

    return docs
