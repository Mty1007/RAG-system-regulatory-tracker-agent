"""Markdown-aware document chunker for regulatory PDFs.

Strategy
--------
1. Split on Markdown headings (``#``, ``##``, ``###``) — preserves the
   natural clause / section structure of regulatory documents.
2. If a section exceeds ``max_words`` after heading-split, apply a secondary
   sliding-window split (``window_words`` words, ``overlap_words`` overlap).
3. Discard chunks shorter than ``min_words`` — these are usually stray table
   labels, page numbers, or empty heading artefacts.

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
# A "word" here is a whitespace-separated token — fast and good enough for
# token-budget estimation without a heavy tokeniser dependency.
DEFAULT_MAX_WORDS = 600       # section larger than this gets secondary split
DEFAULT_WINDOW_WORDS = 400    # sliding-window size
DEFAULT_OVERLAP_WORDS = 50    # overlap between consecutive windows
DEFAULT_MIN_WORDS = 40        # discard chunks smaller than this

# Matches ATX-style Markdown headings: #, ##, or ### (not deeper)
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)


def _word_count(text: str) -> int:
    return len(text.split())


def _sliding_window(
    text: str,
    window: int,
    overlap: int,
) -> Iterator[str]:
    """Yield overlapping word-based windows of *text*."""
    words = text.split()
    step = window - overlap
    if step <= 0:
        step = window
    start = 0
    while start < len(words):
        yield " ".join(words[start : start + window])
        if start + window >= len(words):
            break
        start += step


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
    max_words: int = DEFAULT_MAX_WORDS,
    window_words: int = DEFAULT_WINDOW_WORDS,
    overlap_words: int = DEFAULT_OVERLAP_WORDS,
    min_words: int = DEFAULT_MIN_WORDS,
) -> list[dict]:
    """Split *markdown* into chunks and return a list of chunk dicts.

    Parameters
    ----------
    doc_id:
        Stable document identifier (e.g. ``"sfc-a1b2c3d4e5f6"``).
    markdown:
        Full Markdown string produced by Docling.
    max_words:
        Sections larger than this are further split with a sliding window.
    window_words:
        Word-window size for secondary sliding split.
    overlap_words:
        Overlap in words between consecutive sliding windows.
    min_words:
        Chunks smaller than this are discarded.
    """
    sections = _split_sections(markdown)
    chunks: list[dict] = []
    idx = 0

    for heading, body in sections:
        if _word_count(body) > max_words:
            # secondary sliding-window split
            windows = list(_sliding_window(body, window_words, overlap_words))
        else:
            windows = [body]

        for window_text in windows:
            wc = _word_count(window_text)
            if wc < min_words:
                continue  # discard noise
            chunks.append(
                {
                    "doc_id": doc_id,
                    "chunk_id": f"{doc_id}__c{idx:04d}",
                    "chunk_index": idx,
                    "section_heading": heading,
                    "text": window_text,
                    "token_count": wc,
                }
            )
            idx += 1

    return chunks
