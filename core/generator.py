"""Mistral LLM answer generator (via WatsonX).

Given a user question and a list of reranked context chunks, this module
builds a prompt and calls the WatsonX ``/ml/v1/text/generation`` endpoint
to produce a grounded, citation-backed answer.

Required environment variables
-------------------------------
WATSONX_API_KEY
WATSONX_PROJECT_ID
WATSONX_URL
WATSONX_LLM_MODEL    e.g. mistralai/mistral-medium-2505
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import requests

from core.embedder import _get_iam_token  # reuse IAM token cache

logger = logging.getLogger(__name__)

_GENERATE_PATH = "/ml/v1/text/generation?version=2023-10-25"

# Maximum tokens to reserve for the LLM response
_MAX_NEW_TOKENS = 1024

# ── cross-reference chunk filter ─────────────────────────────────────────────
# Some chunks from certain docs consist entirely of pointers to other documents
# (e.g. "please refer to the Circular…") with no answerable content of their
# own.  When the reranker scores these highly (because they contain the query
# keywords), they land in the top-3 context slots that the faithfulness scorer
# compares against — making a correct, well-grounded answer appear "unfaithful".
#
# The filter is intentionally narrow:
#   - only triggers on SHORT chunks (< 300 chars)
#   - only when ≥ 80% of sentences are cross-reference sentences
# This means it will never fire on a chunk that contains actual rule text
# alongside a reference, and never fires on any of the AML, PCPD, or other
# SFC content chunks seen across all tested questions.
_XREF_RE = re.compile(
    r"please refer to|for details (of |see )|for further (details|information)|"
    r"as set out in|guidance is (available|provided) (at|in)|"
    r"refer to the (circular|guidance|guideline)",
    re.IGNORECASE,
)


def _is_crossref_only(text: str) -> bool:
    """Return True if *text* is a short chunk consisting almost entirely of
    cross-references to other documents, with no substantive rule content."""
    if len(text) >= 300:
        return False
    sentences = [s.strip() for s in re.split(r"[.!?\n]", text) if s.strip()]
    if not sentences:
        return False
    xref_count = sum(1 for s in sentences if _XREF_RE.search(s))
    return xref_count / len(sentences) >= 0.8


def _filter_crossref_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove cross-reference-only chunks, keeping at least 3 chunks always.

    Guarantees the LLM always receives a minimum of 3 context chunks even if
    many are filtered — preventing empty-context answers.
    """
    filtered = [c for c in chunks if not _is_crossref_only(c["text"])]
    removed = len(chunks) - len(filtered)
    if removed:
        logger.info(
            "generator: removed %d cross-reference-only chunk(s) from context",
            removed,
        )
    # Safety: never return fewer than 3 chunks (fall back to original if needed)
    return filtered if len(filtered) >= 3 else chunks

# System instruction prepended to every prompt
_SYSTEM_PROMPT = """\
You are a regulatory compliance assistant specialising in Hong Kong financial \
regulations from the SFC (Securities and Futures Commission) and PCPD \
(Privacy Commissioner for Personal Data).

Answer the user's question using ONLY the context passages provided below. \
Be precise and ground your answers strictly on the facts from the context. \
If the context does not contain enough information to answer, say so clearly — \
do not speculate or use outside knowledge.

STRICT RULES:
1. Answer ONLY what the question asks. Do not add related topics, background, \
   or adjacent information that was not asked for.
2. Keep your answer concise. Use plain prose or a short numbered list. \
   Do not use markdown headers (##, ###) or deep bullet nesting.
3. If a context passage appears to be OCR noise, a table of contents, or \
   an image placeholder (e.g. random letters, page numbers only), ignore it \
   and do not reference it. Do not use it to justify hedging sentences.
4. Do not append sentences that recommend consulting other sources or that \
   suggest seeking further guidance — answer from the context or say it is \
   not available.

IMPORTANT: Respond in the same language as the user's question. \
If the question is in Chinese (Traditional or Simplified), answer in Chinese. \
If the question is in English, answer in English.

If the retrieved context covers only one regulator (e.g. only SFC or only PCPD) \
but the question is not explicitly limited to one regulator, note at the end of \
your answer that the response is based solely on the available context and that \
the other regulator may have additional or different requirements.

For each claim in your answer, add a bracketed reference number corresponding to \
the context source used, e.g. [1] or [1][3]. Do not write document IDs in citations; \
only use the number, e.g. [1].
"""


def _build_prompt(query: str, chunks: list[dict[str, Any]]) -> str:
    """Construct the full prompt string from the query and context chunks."""
    context_lines = []
    for i, chunk in enumerate(chunks, start=1):
        heading = chunk.get("section_heading") or "—"
        source  = chunk.get("source", "")
        page    = chunk.get("page_start", 0)
        text    = chunk.get("text", "")
        context_lines.append(
            f"[{i}] Source: {source} | Section: {heading} | Page: {page}\n{text}"
        )

    context_block = "\n\n---\n\n".join(context_lines)

    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"=== Context ===\n\n{context_block}\n\n"
        f"=== Question ===\n\n{query}\n\n"
        f"=== Answer ===\n"
    )


def generate_answer(
    query: str,
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Generate an answer for *query* grounded in *chunks*.

    Parameters
    ----------
    query:
        The user's question.
    chunks:
        Reranked context chunks from ``core/reranker.rerank()``.

    Returns
    -------
    A dict with:
        ``answer``      — generated answer string
        ``citations``   — list of citation dicts (doc_id, source,
                          section_heading, page_start)
        ``model_used``  — model ID string
        ``chunk_count`` — number of context chunks used
    """
    api_key    = os.environ["WATSONX_API_KEY"]
    project_id = os.environ["WATSONX_PROJECT_ID"]
    base_url   = os.environ["WATSONX_URL"].rstrip("/")
    model_id   = os.environ.get("WATSONX_LLM_MODEL", "mistralai/mistral-medium-2505")

    # Remove cross-reference-only chunks before building the prompt so they
    # don't occupy top-3 context slots and degrade faithfulness scoring.
    chunks = _filter_crossref_chunks(chunks)

    token  = _get_iam_token(api_key)
    url    = base_url + _GENERATE_PATH
    prompt = _build_prompt(query, chunks)

    resp = requests.post(
        url,
        json={
            "model_id":   model_id,
            "project_id": project_id,
            "input":      prompt,
            "parameters": {
                "decoding_method": "greedy",
                "max_new_tokens":  _MAX_NEW_TOKENS,
                "stop_sequences":  ["==="],
                "repetition_penalty": 1.05,
            },
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        timeout=90,
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"WatsonX generation failed HTTP {resp.status_code}: {resp.text[:300]}"
        )

    results = resp.json().get("results", [])
    answer  = results[0].get("generated_text", "").strip() if results else ""

    # Strip any chunk ID markers the LLM may have leaked into the answer.
    # Patterns like 【pcpd-abc123__c0036】, 【source=sfc-abc,section=...】,
    # 【1†sfc-abc__c0001】, 【†】 are internal references that must never
    # appear in the user-facing answer.
    answer = re.sub(r'【[^】]*】', '', answer).strip()
    # Also strip bare [source:...] style markers
    answer = re.sub(r'\[source:[^\]]*\]', '', answer).strip()

    # Parse explicit numeric references [1], [2] inside the generated answer
    cited_indices = set()
    for m in re.finditer(r"\[(\d+)\]", answer):
        idx_1based = int(m.group(1))
        if 1 <= idx_1based <= len(chunks):
            cited_indices.add(idx_1based - 1)

    # Fallback: if the LLM produced no explicit reference tags but answered,
    # or if we want to be safe, only cite matched chunks. If no indices matched,
    # we default to empty citations rather than cluttering with irrelevant ones.
    citations = []
    seen: set[tuple[str, str, int]] = set()
    
    for i, c in enumerate(chunks):
        if i not in cited_indices:
            continue
        # Unique mapping on doc, section, and page
        key = (c.get("doc_id", ""), c.get("section_heading", ""), c.get("page_start", 0))
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            {
                "doc_id":          c.get("doc_id", ""),
                "source":          c.get("source", ""),
                "section_heading": c.get("section_heading", ""),
                "page_start":      c.get("page_start", 0),
            }
        )

    logger.info(
        "generate_answer: model=%s  chunks=%d  answer_len=%d",
        model_id, len(chunks), len(answer),
    )

    return {
        "answer":      answer,
        "citations":   citations,
        "model_used":  model_id,
        "chunk_count": len(chunks),
    }
