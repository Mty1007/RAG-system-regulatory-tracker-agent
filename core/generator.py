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
from typing import Any

import requests

from core.embedder import _get_iam_token  # reuse IAM token cache

logger = logging.getLogger(__name__)

_GENERATE_PATH = "/ml/v1/text/generation?version=2023-10-25"

# Maximum tokens to reserve for the LLM response
_MAX_NEW_TOKENS = 1024

import re

# System instruction prepended to every prompt
_SYSTEM_PROMPT = """\
You are a regulatory compliance assistant specialising in Hong Kong financial \
regulations from the SFC (Securities and Futures Commission) and PCPD \
(Privacy Commissioner for Personal Data).

Answer the user's question using ONLY the context passages provided below. \
Be precise and ground your answers strictly on the facts from the context. \
If the context does not contain enough information to answer, say so clearly — \
do not speculate or use outside knowledge.

IMPORTANT: Respond in the same language as the user's question. \
If the question is in Chinese (Traditional or Simplified), answer in Chinese. \
If the question is in English, answer in English.

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
