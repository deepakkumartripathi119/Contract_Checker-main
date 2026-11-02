"""Shared keyword index used by every vector-store backend.

Dense search can change with the database. This keyword search stays the same
so SQLite and Postgres rank text the same way.
"""

from __future__ import annotations

import re
from typing import Optional

from rank_bm25 import BM25Okapi


def tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def build_index(chunks: list) -> Optional[BM25Okapi]:
    if not chunks:
        return None
    corpus = [tokenize(f"{chunk.breadcrumb} {chunk.text}") for chunk in chunks]
    index = BM25Okapi(corpus)
    for word, weight in index.idf.items():
        if weight <= 0:
            index.idf[word] = 0.5
    return index


def rank(index: Optional[BM25Okapi], chunks: list, query_text: str, top_k: int) -> list:
    if index is None or not chunks:
        return []
    tokens = tokenize(query_text)
    if not tokens:
        return []
    scores = index.get_scores(tokens)
    scored = [
        (float(scores[index_]), chunks[index_])
        for index_ in range(len(chunks))
        if scores[index_] > 0
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[:top_k]
