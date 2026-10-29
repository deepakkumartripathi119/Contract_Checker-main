"""
pipeline/nodes/document_parser.py — Layout-aware PDF → structured chunks.

Responsibilities
----------------
1. Convert a PDF to Markdown using pymupdf4llm (preserves headings, tables,
   page markers, numbered lists).
2. Split Markdown by heading boundaries — NOT by character count.
   Supports arbitrary nesting depth:
     1.          → top-level section   (parent chunk)
     1.1         → sub-section         (child of 1)
     1.1.1       → sub-sub-section     (child of 1.1)
     # / ## /### → Markdown headings   (same treatment)
3. Prepend a breadcrumb header to each chunk:
     "[Doc: Nexify Policy | Section 2: Payment Terms | Page 3]"
4. Build a parent-child index:
     - child chunks  → embedded + searched (small, sharp vectors)
     - parent chunks → stored for LLM context (full section text)
5. Fallback: if 0 headings detected (scanned / flat PDF) → return the full
   Markdown as one chunk and let the caller use LLM extraction.

org_id
------
Every function accepts org_id so chunks are always org-scoped.
Currently defaults to the value of POLICY_ORG_ID env var ("nexify" if unset).
Multi-tenant: callers pass org_id per request — no other changes needed.

Public API
----------
parse_policy_pdf(pdf_bytes, doc_name, org_id)  → list[Chunk]
    Full parent+child tree for a policy document.

parse_vendor_contract(pdf_bytes, doc_name)     → list[dict]
    Flat list of {"text": ..., "clause_type": ..., "metadata": ...} dicts
    for feeding into the compliance pipeline.
    Falls back to LLM extraction if no headings found.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.vector_store import Chunk


# ---------------------------------------------------------------------------
# Heading patterns — order matters (most specific first)
# ---------------------------------------------------------------------------

# Matches: "1.1.1 Title", "2.3 Title", "1. Title", "2) Title", "#", "##", "###"
_HEADING_PATTERNS = [
    re.compile(r"^(\d+(?:\.\d+)*)[\.\)]?\s+(.+)$", re.MULTILINE),   # numbered: 1. / 1.1 / 1.1.1
    re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE),                  # Markdown ATX headings
]

# A line is a heading if it matches any of the above
_ANY_HEADING = re.compile(
    r"^(?:\d+(?:\.\d+)*[\.\)]?\s+.+|#{1,4}\s+.+)$", re.MULTILINE
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _doc_id(pdf_bytes: bytes) -> str:
    """Stable SHA256 fingerprint for a PDF file."""
    return hashlib.sha256(pdf_bytes).hexdigest()[:16]


def _pdf_to_markdown(pdf_bytes: bytes) -> str:
    """
    Convert PDF bytes to Markdown using pymupdf4llm.
    Falls back to plain text via pypdf if pymupdf4llm is not installed.
    """
    try:
        import pymupdf4llm          # type: ignore
        import pymupdf              # type: ignore  (fitz)
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        md = pymupdf4llm.to_markdown(doc)
        return md
    except ImportError:
        # Graceful fallback — install pymupdf4llm for full layout preservation
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for i, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            pages.append(f"[Page {i}]\n{text}")
        return "\n\n".join(pages)


def _detect_page(text_before: str) -> int:
    """
    Find the last [Page N] marker before a given text position.
    Returns 0 if none found.
    """
    matches = list(re.finditer(r"\[Page (\d+)\]", text_before))
    if matches:
        return int(matches[-1].group(1))
    return 0


def _heading_depth(heading_str: str) -> int:
    """
    Return the nesting depth of a heading string.
    "1"     → 1
    "1.1"   → 2
    "1.1.1" → 3
    "#"     → 1, "##" → 2, "###" → 3
    """
    if heading_str.startswith("#"):
        return len(heading_str)
    return len(heading_str.split("."))


def _line_heading_depth(heading_line: str) -> int:
    """Return nesting depth from a complete numbered or Markdown heading line."""
    num_match = re.match(r"^(\d+(?:\.\d+)*)[\.\)]?\s+(.+)$", heading_line)
    if num_match:
        return _heading_depth(num_match.group(1))
    md_match = re.match(r"^(#{1,4})\s+(.+)$", heading_line)
    if md_match:
        embedded_num = re.match(
            r"^\*{0,2}(\d+(?:\.\d+)*)[\.\)]?\s+", md_match.group(2)
        )
        if embedded_num:
            return _heading_depth(embedded_num.group(1))
        return _heading_depth(md_match.group(1))
    return 0


def _clean_heading_title(title: str) -> str:
    """Remove common Markdown emphasis around extracted heading titles."""
    return re.sub(r"[*_`]+", "", title).strip()


def _make_breadcrumb(doc_name: str, section_stack: list[str], page: int) -> str:
    """
    Build a human-readable breadcrumb injected at the top of each chunk.

    Example: "[Doc: Nexify Policy | Section 1: Payment Terms | Section 1.1: Late Fees | Page 2]"
    """
    parts = [f"Doc: {doc_name}"]
    for s in section_stack:
        parts.append(f"Section {s}")
    if page:
        parts.append(f"Page {page}")
    return "[" + " | ".join(parts) + "]"


# ---------------------------------------------------------------------------
# Core split logic
# ---------------------------------------------------------------------------

@dataclass
class _RawSection:
    """Intermediate structure before converting to Chunk."""
    heading_num: str    # e.g. "1.1" or "##"
    title: str
    text: str           # full text including heading line
    page: int
    depth: int


def _split_by_headings(markdown: str, doc_name: str) -> list[_RawSection]:
    """
    Split Markdown into sections at every heading boundary.
    Returns a flat list of _RawSection in document order.
    """
    # Find all heading matches with their positions
    heading_matches = list(_ANY_HEADING.finditer(markdown))

    if not heading_matches:
        return []

    sections: list[_RawSection] = []

    for i, match in enumerate(heading_matches):
        start = match.start()
        heading_line = match.group(0)

        # Parse heading number and title
        num_match = re.match(r"^(\d+(?:\.\d+)*)[\.\)]?\s+(.+)$", heading_line)
        md_match = re.match(r"^(#{1,4})\s+(.+)$", heading_line)

        if num_match:
            heading_num = num_match.group(1)
            title = num_match.group(2).strip()
        elif md_match:
            heading_num = md_match.group(1)   # e.g. "##"
            title = _clean_heading_title(md_match.group(2))
            numbered_title = re.match(
                r"^(\d+(?:\.\d+)*)[\.\)]?\s+(.+)$", title
            )
            if numbered_title:
                heading_num = numbered_title.group(1)
                title = numbered_title.group(2).strip()
        else:
            heading_num = str(i)
            title = heading_line.strip()

        depth = _heading_depth(heading_num)
        if depth == 1:
            end = next(
                (
                    candidate.start()
                    for candidate in heading_matches[i + 1:]
                    if _line_heading_depth(candidate.group(0)) == 1
                ),
                len(markdown),
            )
        else:
            end = heading_matches[i + 1].start() if i + 1 < len(heading_matches) else len(markdown)

        section_text = markdown[start:end].strip()
        page = _detect_page(markdown[:start])

        sections.append(_RawSection(
            heading_num=heading_num,
            title=title,
            text=section_text,
            page=page,
            depth=depth,
        ))

    # A standalone first Markdown heading is commonly the document title,
    # not a mandatory policy section. Numbered sections that follow it are
    # promoted to their own top-level sections above.
    if (
        sections
        and sections[0].heading_num.startswith("#")
        and not re.match(r"^\d", sections[0].title)
        and any(re.match(r"^\d", section.heading_num) for section in sections[1:])
    ):
        sections = sections[1:]

    return sections


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_policy_pdf(
    pdf_bytes: bytes,
    doc_name: str,
    org_id: Optional[str] = None,
) -> list[Chunk]:
    """
    Parse a policy PDF into a parent-child Chunk tree stored per org.

    Parent chunks  → full section text (for LLM context window)
    Child chunks   → individual sub-sections (for embedding + search)

    If no headings are found the entire document becomes one parent chunk
    and one child chunk pointing to it.

    Parameters
    ----------
    pdf_bytes : raw bytes of the uploaded PDF
    doc_name  : human-readable name (used in breadcrumbs)
    org_id    : organisation identifier; falls back to POLICY_ORG_ID env var

    Returns
    -------
    Flat list of Chunk objects (mix of parents and children).
    Caller should:
      1. Embed all child chunks in one batch call
      2. Call vector_store.add_chunks(org_id, chunks)
    """
    org_id = org_id or os.getenv("POLICY_ORG_ID", "nexify")
    did = _doc_id(pdf_bytes)
    markdown = _pdf_to_markdown(pdf_bytes)
    sections = _split_by_headings(markdown, doc_name)

    chunks: list[Chunk] = []

    if not sections:
        # Flat / scanned PDF — single parent + single child (same text)
        parent_id = str(uuid.uuid4())
        parent = Chunk(
            id=parent_id,
            org_id=org_id,
            doc_id=did,
            parent_id=None,
            chunk_type="parent",
            text=markdown,
            breadcrumb=f"[Doc: {doc_name} | Page 1]",
            metadata={"doc_name": doc_name, "flat": True},
        )
        child = Chunk(
            id=str(uuid.uuid4()),
            org_id=org_id,
            doc_id=did,
            parent_id=parent_id,
            chunk_type="child",
            text=markdown,
            breadcrumb=f"[Doc: {doc_name} | Page 1]",
            metadata={"doc_name": doc_name, "flat": True},
        )
        return [parent, child]

    # Map depth → most-recent parent id at that depth
    # depth 1 sections are top-level parents; deeper sections are children
    depth_to_parent_id: dict[int, str] = {}

    section_stack: list[tuple[int, str]] = []

    for sec in sections:
        section_stack = [item for item in section_stack if item[0] < sec.depth]
        section_stack.append((sec.depth, f"{sec.heading_num}: {sec.title}"))
        breadcrumb = _make_breadcrumb(
            doc_name,
            [title for _, title in section_stack],
            sec.page,
        )

        if sec.depth == 1:
            # Top-level section → always a parent chunk
            chunk_id = str(uuid.uuid4())
            depth_to_parent_id[1] = chunk_id
            # Clear deeper depth mappings (new top-level section starts)
            for d in list(depth_to_parent_id):
                if d > 1:
                    depth_to_parent_id.pop(d)

            chunk = Chunk(
                id=chunk_id,
                org_id=org_id,
                doc_id=did,
                parent_id=None,
                chunk_type="parent",
                text=sec.text,
                breadcrumb=breadcrumb,
                metadata={
                    "doc_name": doc_name,
                    "heading": sec.title,
                    "heading_num": sec.heading_num,
                    "page": sec.page,
                    "depth": sec.depth,
                },
            )
            chunks.append(chunk)

            # Also add as a child so it is searchable
            child_id = str(uuid.uuid4())
            child = Chunk(
                id=child_id,
                org_id=org_id,
                doc_id=did,
                parent_id=chunk_id,
                chunk_type="child",
                text=sec.text,
                breadcrumb=breadcrumb,
                metadata={
                    "doc_name": doc_name,
                    "heading": sec.title,
                    "heading_num": sec.heading_num,
                    "page": sec.page,
                    "depth": sec.depth,
                },
            )
            chunks.append(child)

        else:
            # Nested section → child chunk pointing to nearest shallower parent
            parent_depth = sec.depth - 1
            # Walk up the stack to find a valid parent
            while parent_depth > 0 and parent_depth not in depth_to_parent_id:
                parent_depth -= 1

            parent_id = depth_to_parent_id.get(parent_depth)
            chunk_id = str(uuid.uuid4())
            depth_to_parent_id[sec.depth] = chunk_id

            chunk = Chunk(
                id=chunk_id,
                org_id=org_id,
                doc_id=did,
                parent_id=parent_id,
                chunk_type="child",
                text=sec.text,
                breadcrumb=breadcrumb,
                metadata={
                    "doc_name": doc_name,
                    "heading": sec.title,
                    "heading_num": sec.heading_num,
                    "page": sec.page,
                    "depth": sec.depth,
                },
            )
            chunks.append(chunk)

    return chunks


def parse_vendor_contract(
    pdf_bytes: bytes,
    doc_name: str,
) -> tuple[list[dict], bool]:
    """
    Parse a vendor contract PDF into a flat list of clause dicts.

    Each dict:
      {
        "text":        str,   # full clause text
        "clause_type": str,   # heading title (e.g. "Payment Terms")
        "metadata":    dict,  # page, heading_num, breadcrumb, …
      }

    Returns
    -------
    (clauses, used_llm_fallback)
      clauses          : list of clause dicts
      used_llm_fallback: True if no headings found and caller should
                         trigger LLM extraction instead

    The caller (app.py) checks used_llm_fallback and routes accordingly.
    """
    markdown = _pdf_to_markdown(pdf_bytes)
    sections = _split_by_headings(markdown, doc_name)

    if not sections:
        # No headings detected — signal caller to use LLM extraction
        return [{"text": markdown, "clause_type": "Full Document", "metadata": {}}], True

    clauses = []
    for sec in sections:
        breadcrumb = _make_breadcrumb(doc_name, [f"{sec.heading_num}: {sec.title}"], sec.page)
        clauses.append({
            "text": sec.text,
            "clause_type": sec.title,
            "metadata": {
                "doc_name": doc_name,
                "heading_num": sec.heading_num,
                "page": sec.page,
                "breadcrumb": breadcrumb,
                "depth": sec.depth,
            },
        })

    return clauses, False
