"""Markdown-aware document chunker for regulatory PDFs.

Strategy
--------
1. Split on Markdown headings (``#``, ``##``, ``###``) — preserves the
   natural clause / section structure of regulatory documents.
2. If a section exceeds ``max_chars`` after heading-split, apply a secondary
   sliding-window split by character count.  Character-based splitting is
   essential for mixed English/Chinese regulatory text — word-based splitting
   does not control token count reliably because CJK characters have no
   whitespace between them and tokenise at 2–4 tokens each.
3. Discard chunks shorter than ``min_chars`` — these are usually stray table
   labels, page numbers, or empty heading artefacts.

Token budget
------------
ibm/granite-embedding-278m-multilingual has a hard 512-token limit.
Worst case: dense Chinese text at ~1 token per 1.5 chars → 480 chars ≈ 320 tokens.
English regulatory text: ~1 token per 4 chars → 480 chars ≈ 120 tokens.
480-char chunks stay well under 512 tokens for any mix of EN/ZH content.

Each returned chunk dict has the shape expected by
``store/astra_chunk_store.py``::

    {
        "doc_id":          str,   # e.g. "sfc-a1b2c3d4e5f6"
        "chunk_id":        str,   # "{doc_id}__c{n:04d}"
        "chunk_index":     int,   # 0-based, unique within document
        "section_heading": str,   # nearest heading above this chunk
        "text":            str,   # raw chunk text
        "token_count":     int,   # rough word-based estimate
    }

``page_start`` and ``source`` are injected later by the pipeline script
(``scripts/run_chunk.py``) which has access to the full doc metadata.
"""

from __future__ import annotations

import re
from typing import Iterator

# ── tunables ─────────────────────────────────────────────────────────────────
# Character-based limits — reliable for both English and Chinese text.
#
# ibm/granite-embedding-278m-multilingual: 512-token limit.
# Worst-case density: ~1 token per 1.5 chars (dense CJK).
# 480 chars → ~320 tokens worst-case; comfortably under 512.
DEFAULT_MAX_CHARS    = 480   # section larger than this gets secondary split
DEFAULT_WINDOW_CHARS = 480   # sliding-window size in characters
DEFAULT_OVERLAP_CHARS = 60   # overlap between consecutive windows (chars)
DEFAULT_MIN_CHARS    = 80    # discard chunks smaller than this

# Matches ATX-style Markdown headings: #, ##, or ### (not deeper)
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)


def _sliding_window_chars(
    text: str,
    window: int,
    overlap: int,
) -> Iterator[str]:
    """Yield overlapping character-based windows of *text*.

    Splits are made at whitespace boundaries where possible so words/CJK
    tokens are not cut mid-character.
    """
    step = window - overlap
    if step <= 0:
        step = window

    start = 0
    while start < len(text):
        end = min(start + window, len(text))

        # Snap end to a whitespace boundary (scan back up to 20 chars)
        # so we don't cut mid-word in English text.  CJK text has no spaces
        # so this is a no-op there — that's fine, character boundaries are
        # safe for CJK.
        if end < len(text):
            snap = text.rfind(" ", start, end)
            if snap > start + window // 2:
                end = snap

        chunk = text[start:end].strip()
        if chunk:
            yield chunk

        if end >= len(text):
            break
        start = start + step


def _split_sections(markdown: str) -> list[tuple[str, str]]:
    """Return ``[(heading, body), …]`` by splitting on ATX headings.

    Text before the first heading is attributed to an empty heading string.
    """
    sections: list[tuple[str, str]] = []
    last_heading = ""
    last_end = 0

    for m in _HEADING_RE.finditer(markdown):
        body = markdown[last_end : m.start()].strip()
        if body:
            sections.append((last_heading, body))
        last_heading = m.group(2).strip()
        last_end = m.end()

    # tail after the last heading
    tail = markdown[last_end:].strip()
    if tail:
        sections.append((last_heading, tail))

    return sections


def chunk_markdown(
    doc_id: str,
    markdown: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    window_chars: int = DEFAULT_WINDOW_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
) -> list[dict]:
    """Split *markdown* into chunks and return a list of chunk dicts.

    Parameters
    ----------
    doc_id:
        Stable document identifier (e.g. ``"sfc-a1b2c3d4e5f6"``).
    markdown:
        Full Markdown string produced by Docling / TextExtractionsV2.
    max_chars:
        Sections larger than this (in characters) are further split with a
        sliding character window.
    window_chars:
        Character-window size for secondary sliding split.
    overlap_chars:
        Overlap in characters between consecutive sliding windows.
    min_chars:
        Chunks smaller than this (in characters) are discarded as noise.
    """
    sections = _split_sections(markdown)
    chunks: list[dict] = []
    idx = 0

    for heading, body in sections:
        if len(body) > max_chars:
            # secondary sliding-window split by character count
            windows = list(_sliding_window_chars(body, window_chars, overlap_chars))
        else:
            windows = [body]

        for window_text in windows:
            if len(window_text) < min_chars:
                continue  # discard noise
            chunks.append(
                {
                    "doc_id": doc_id,
                    "chunk_id": f"{doc_id}__c{idx:04d}",
                    "chunk_index": idx,
                    "section_heading": heading,
                    "text": window_text,
                    "token_count": len(window_text.split()),  # word count estimate
                }
            )
            idx += 1

    return chunks
