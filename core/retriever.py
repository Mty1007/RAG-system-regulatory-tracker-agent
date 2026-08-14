"""Hybrid retriever for the RAG pipeline.

Combines semantic (ANN vector) search and keyword (BM25 text) search against
the AstraDB ``chunks`` collection using AstraDB's native ``find_and_rerank()``
API, which performs hybrid retrieval and reranking in a single call.

Why hybrid?
-----------
Regulatory documents contain formal legal language (clause numbers, defined
terms) that keyword search handles well, *and* conceptual questions
("what are the requirements for client asset segregation?") that semantic
search handles well.  AstraDB's built-in NVIDIA reranker
(nvidia/llama-3.2-nv-rerankqa-1b-v2) then reranks the merged results.

Embeddings are generated automatically by AstraDB via ``$vectorize`` —
no WatsonX embedding call is needed for retrieval either.

Cross-lingual retrieval
-----------------------
When a Chinese query is detected, an English translation is produced via
WatsonX and used for a second retrieval pass.  Results from both passes are
merged (deduplicated by chunk_id) before reranking so that English-only
regulatory documents remain reachable from Chinese queries.

Required environment variables
-------------------------------
ASTRA_DB_APPLICATION_TOKEN
ASTRA_DB_API_ENDPOINT
ASTRA_DB_KEYSPACE            (optional, default "default_keyspace")
WATSONX_API_KEY              (for Chinese query translation)
WATSONX_PROJECT_ID
WATSONX_URL
WATSONX_LLM_MODEL
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import requests

from astrapy import DataAPIClient

logger = logging.getLogger(__name__)

CHUNKS_COLLECTION = "chunks"

# Matches any CJK Unified Ideograph
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _is_chinese(text: str) -> bool:
    """Return True if *text* contains a meaningful amount of Chinese characters."""
    return len(_CJK_RE.findall(text)) >= 3


def _translate_to_english(text: str) -> str:
    """Translate *text* to English using WatsonX LLM.

    Returns the original text unchanged on any error so retrieval degrades
    gracefully rather than raising.
    """
    try:
        from core.embedder import _get_iam_token  # reuse IAM token cache

        api_key    = os.environ["WATSONX_API_KEY"]
        project_id = os.environ["WATSONX_PROJECT_ID"]
        base_url   = os.environ["WATSONX_URL"].rstrip("/")
        model_id   = os.environ.get("WATSONX_LLM_MODEL", "mistralai/mistral-medium-2505")
        token      = _get_iam_token(api_key)

        prompt = (
            "Translate the following question into English. "
            "Output only the English translation, nothing else.\n\n"
            f"Question: {text}\n\nEnglish translation:"
        )
        resp = requests.post(
            base_url + "/ml/v1/text/generation?version=2023-10-25",
            json={
                "model_id":   model_id,
                "project_id": project_id,
                "input":      prompt,
                "parameters": {
                    "decoding_method": "greedy",
                    "max_new_tokens":  80,
                    "stop_sequences":  ["\n"],
                },
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            timeout=30,
        )
        if resp.status_code == 200:
            translated = (
                resp.json().get("results", [{}])[0]
                .get("generated_text", "")
                .strip()
            )
            if translated:
                logger.info("translate: '%s' → '%s'", text[:60], translated[:60])
                return translated
    except Exception as exc:
        logger.warning("translate failed, using original query: %s", exc)
    return text

def _get_collection():
    token    = os.environ["ASTRA_DB_APPLICATION_TOKEN"]
    endpoint = os.environ["ASTRA_DB_API_ENDPOINT"].rstrip("/")
    keyspace = os.environ.get("ASTRA_DB_KEYSPACE", "default_keyspace")
    client   = DataAPIClient(token)
    database = client.get_database(endpoint, keyspace=keyspace)
    return database.get_collection(CHUNKS_COLLECTION)


def retrieve(
    query: str,
    *,
    source_filter: Optional[str] = None,
    top_n: int = 20,
    top_k: int = 8,
) -> list[dict[str, Any]]:
    """Return the top-K most relevant chunks for *query*.

    Uses AstraDB ``find_and_rerank()`` which performs hybrid retrieval
    (semantic ANN + BM25 keyword) and reranking in a single API call using
    the collection's built-in NVIDIA reranker
    (nvidia/llama-3.2-nv-rerankqa-1b-v2).

    Parameters
    ----------
    query:
        User's natural-language question.
    source_filter:
        Optional source restriction: ``"SFC"`` or ``"PCPD"``.
        Pass ``None`` to search across all sources.
    top_n:
        Number of candidates to retrieve before reranking.
    top_k:
        Final number of chunks to return after reranking.

    Returns
    -------
    List of chunk dicts (without ``$vector``), sorted by descending rerank
    score.  Each dict has: ``_id``, ``doc_id``, ``source``, ``chunk_index``,
    ``section_heading``, ``page_start``, ``text``, ``token_count``,
    ``rerank_score``.
    """
    collection = _get_collection()

    # ── build optional source pre-filter ─────────────────────────────────────
    filter_doc: dict = {}
    if source_filter:
        filter_doc["source"] = source_filter.upper()

    def _run_search(q: str) -> list[dict]:
        cursor = collection.find_and_rerank(
            filter_doc,
            sort={"$hybrid": {"$vectorize": q, "$lexical": q}},
            rerank_query=q,
            rerank_on="text",
            limit=top_k,
            hybrid_limits=top_n,
            projection={"$vectorize": 0},
            include_scores=True,
        )
        out = []
        for r in cursor:
            doc = dict(r.document)
            scores = r.scores or {}
            doc["rerank_score"] = scores.get("$rerank", 0.0)
            doc["rrf_score"]    = scores.get("$rrf", 0.0)
            out.append(doc)
        return out

    # ── primary search ────────────────────────────────────────────────────────
    chunks = _run_search(query)

    # ── cross-lingual: also search with English translation for Chinese queries
    if _is_chinese(query):
        en_query = _translate_to_english(query)
        if en_query != query:
            en_chunks = _run_search(en_query)
            # merge, deduplicating by _id — keep highest rerank_score
            seen: dict[str, dict] = {c["_id"]: c for c in chunks}
            for c in en_chunks:
                cid = c["_id"]
                if cid not in seen or c["rerank_score"] > seen[cid]["rerank_score"]:
                    seen[cid] = c
            # re-sort by rerank_score descending, cap at top_k
            chunks = sorted(seen.values(), key=lambda x: x["rerank_score"], reverse=True)[:top_k]
            logger.info("retrieve: merged %d EN chunks → %d total", len(en_chunks), len(chunks))

    logger.info(
        "retrieve: query=%r  source=%s  returned=%d",
        query[:60],
        source_filter or "ALL",
        len(chunks),
    )
    return chunks
