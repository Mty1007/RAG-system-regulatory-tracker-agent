"""COS full-document Markdown fallback retriever.

Chunks are 480-character windows.  For questions spanning multiple sections,
or when the top reranked chunk's score is weak, the best answer may sit
between chunk boundaries.  This module supplements the AstraDB chunk results
with wider Markdown passages downloaded directly from COS.

When it fires
-------------
The fallback is triggered by api/routers/chat.py when the maximum rerank_score
across all retrieved chunks is below COS_FALLBACK_THRESHOLD (default 0.35).

What it does
------------
1. Collect the unique doc_ids of the top-3 AstraDB chunks.
2. Download ``transformed/<doc_id>.md`` from COS for each doc (the canonical
   Docling Markdown output produced by the OCR pipeline).
3. For each chunk, locate its ``section_heading`` inside the full Markdown
   and extract a ±2-section window around it.
4. Return these expanded passages as synthetic chunk dicts that can be
   appended to the AstraDB context list before generate_answer() is called.
   They are labelled with ``source_type="cos_fallback"`` so the generator
   and eval modules can distinguish them from regular AstraDB chunks.

Guard rails
-----------
* Only activates when the ``USE_COS`` environment variable is set.
  Local dev without COS credentials is unaffected — the function returns []
  silently.
* Maximum 3 doc_ids are processed (one COS GET per doc).
* Total additional passages are capped at ``max_extra`` (default 3) so the
  LLM prompt budget is not blown.
* Downloaded Markdown is not cached between requests — COS sequential reads
  are cheap and avoiding an in-process cache prevents stale state.
* Only reads from ``transformed/`` prefix — raw PDFs are never fetched on the
  hot path; Docling already converted them to clean Markdown.

Required environment variables
-------------------------------
USE_COS              any non-empty value enables COS mode
COS_API_KEY          IBM IAM API key
COS_INSTANCE_CRN     CRN of the COS service instance
COS_ENDPOINT         Regional endpoint URL
COS_BUCKET           Bucket containing the ``transformed/`` prefix
COS_FALLBACK_THRESHOLD   float 0-1, default 0.35
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_FALLBACK_THRESHOLD = 0.35
_MAX_DOCS = 3
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)


def _fallback_threshold() -> float:
    try:
        return float(os.environ.get("COS_FALLBACK_THRESHOLD", _DEFAULT_FALLBACK_THRESHOLD))
    except (TypeError, ValueError):
        return _DEFAULT_FALLBACK_THRESHOLD


def _make_cos_client():
    """Build an ibm_boto3 S3 client from environment variables."""
    import ibm_boto3  # type: ignore
    from ibm_botocore.client import Config  # type: ignore

    return ibm_boto3.client(
        "s3",
        ibm_api_key_id=os.environ["COS_API_KEY"],
        ibm_service_instance_id=os.environ["COS_INSTANCE_CRN"],
        config=Config(signature_version="oauth"),
        endpoint_url=os.environ["COS_ENDPOINT"],
    )


def _download_markdown(doc_id: str) -> Optional[str]:
    """Download ``transformed/<doc_id>.md`` from COS.

    Returns None on any error so the caller can skip this doc gracefully.
    """
    key = f"transformed/{doc_id}.md"
    bucket = os.environ.get("COS_BUCKET", "")
    if not bucket:
        logger.warning("cos_retriever: COS_BUCKET not set")
        return None
    try:
        client = _make_cos_client()
        obj = client.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("cos_retriever: failed to download %s: %s", key, exc)
        return None


def _extract_window(
    markdown: str,
    section_heading: str,
    context_sections: int = 2,
) -> str:
    """Extract a ±context_sections window around *section_heading* in *markdown*.

    If the heading is not found, returns the first ``context_sections * 2``
    sections of the document as a fallback (the document introduction is
    often relevant for regulatory queries).
    """
    # Build a list of (heading_text, body) pairs from the full Markdown.
    sections: list[tuple[str, str]] = []
    last_heading = ""
    last_end = 0

    for m in _HEADING_RE.finditer(markdown):
        body = markdown[last_end: m.start()].strip()
        if body:
            sections.append((last_heading, body))
        last_heading = m.group(2).strip()
        last_end = m.end()

    tail = markdown[last_end:].strip()
    if tail:
        sections.append((last_heading, tail))

    if not sections:
        return markdown[:2000]  # degenerate: no headings found

    # Find the best-matching section index (exact match first, then substring).
    target_idx: Optional[int] = None
    if section_heading:
        for i, (h, _) in enumerate(sections):
            if h.lower() == section_heading.lower():
                target_idx = i
                break
        if target_idx is None:
            for i, (h, _) in enumerate(sections):
                if section_heading.lower() in h.lower() or h.lower() in section_heading.lower():
                    target_idx = i
                    break

    if target_idx is None:
        target_idx = 0

    lo = max(0, target_idx - context_sections)
    hi = min(len(sections), target_idx + context_sections + 1)

    parts = []
    for h, body in sections[lo:hi]:
        parts.append(f"## {h}\n\n{body}" if h else body)

    result = "\n\n".join(parts)
    # Hard cap: never return more than 4000 chars so the LLM prompt budget
    # is not blown even on very long section windows.
    return result[:4000]


def _top_rerank_score(chunks: list[dict[str, Any]]) -> float:
    if not chunks:
        return 0.0
    return max(c.get("rerank_score", 0.0) for c in chunks)


# ── public API ────────────────────────────────────────────────────────────────

def fetch_fallback_passages(
    chunks: list[dict[str, Any]],
    *,
    max_extra: int = 3,
) -> list[dict[str, Any]]:
    """Return expanded COS Markdown passages supplementing *chunks*.

    Returns an empty list if:
    * ``USE_COS`` env var is not set
    * top rerank_score >= COS_FALLBACK_THRESHOLD
    * any COS download fails (logged as WARNING, not raised)

    Parameters
    ----------
    chunks:
        AstraDB chunks already retrieved and reranked.
    max_extra:
        Maximum number of additional COS passages to return.

    Returns
    -------
    List of synthetic chunk dicts with shape compatible with generate_answer():
        doc_id, source, section_heading, page_start, text, token_count,
        rerank_score, source_type="cos_fallback"
    """
    if not os.environ.get("USE_COS"):
        return []

    if _top_rerank_score(chunks) >= _fallback_threshold():
        logger.debug(
            "cos_retriever: top rerank_score %.3f >= threshold %.3f — skipping fallback",
            _top_rerank_score(chunks),
            _fallback_threshold(),
        )
        return []

    logger.info(
        "cos_retriever: weak retrieval (top_score=%.3f < threshold=%.3f) — fetching COS fallback",
        _top_rerank_score(chunks),
        _fallback_threshold(),
    )

    # Collect unique doc_ids from top chunks, preserving rank order.
    seen_docs: list[str] = []
    for c in chunks:
        did = c.get("doc_id", "")
        if did and did not in seen_docs:
            seen_docs.append(did)
        if len(seen_docs) >= _MAX_DOCS:
            break

    extra_passages: list[dict[str, Any]] = []

    for doc_id in seen_docs:
        if len(extra_passages) >= max_extra:
            break

        markdown = _download_markdown(doc_id)
        if not markdown:
            continue

        # Find the section_heading used by the best-scoring chunk for this doc.
        doc_chunks = [c for c in chunks if c.get("doc_id") == doc_id]
        heading = doc_chunks[0].get("section_heading", "") if doc_chunks else ""

        window_text = _extract_window(markdown, heading)
        if not window_text.strip():
            continue

        extra_passages.append(
            {
                "doc_id":          doc_id,
                "chunk_id":        f"{doc_id}__cos_fallback",
                "chunk_index":     -1,
                "source":          doc_chunks[0].get("source", "") if doc_chunks else "",
                "section_heading": heading,
                "page_start":      doc_chunks[0].get("page_start", 0) if doc_chunks else 0,
                "text":            window_text,
                "token_count":     len(window_text.split()),
                "rerank_score":    0.0,   # not reranked — supplemental only
                "source_type":     "cos_fallback",
            }
        )
        logger.info(
            "cos_retriever: added fallback passage for doc_id=%s heading=%r  chars=%d",
            doc_id,
            heading[:50],
            len(window_text),
        )

    return extra_passages
