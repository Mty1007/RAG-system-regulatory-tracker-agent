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

# ── keyword-triggered doc injection ──────────────────────────────────────────
# Some queries use vocabulary that the NVIDIA reranker consistently scores low
# even when the content is highly relevant (e.g. "internal audit" maps to SFC's
# "operational review function" / "Internal Control Guidelines").
# When a query matches one of these patterns, always fetch the top-2 chunks
# from the mapped doc_id and inject them into the result set, bypassing the
# reranker score floor.
_KEYWORD_DOC_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"internal audit|audit function|audit department|audit committee",
                re.IGNORECASE), "sfc-a90505b192cd"),  # SFC Internal Control Guidelines
]


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

def _expand_query(text: str) -> str:
    """Rewrite *text* with regulatory synonyms using WatsonX LLM.

    Targets vocabulary mismatches between user language and SFC/PCPD document
    terminology — e.g. "internal audit" → "internal audit review function
    operational review compliance oversight".

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
            "You are a Hong Kong financial regulatory expert. "
            "Rewrite the following query by adding relevant synonyms and "
            "alternative phrasings used in SFC and PCPD regulatory documents. "
            "Output ONLY the expanded query as a single line, nothing else.\n\n"
            f"Query: {text}\n\nExpanded query:"
        )
        resp = requests.post(
            base_url + "/ml/v1/text/generation?version=2023-10-25",
            json={
                "model_id":   model_id,
                "project_id": project_id,
                "input":      prompt,
                "parameters": {
                    "decoding_method": "greedy",
                    "max_new_tokens":  60,
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
            expanded = (
                resp.json().get("results", [{}])[0]
                .get("generated_text", "")
                .strip()
            )
            if expanded and expanded != text:
                logger.info("expand_query: '%s' → '%s'", text[:60], expanded[:60])
                return expanded
    except Exception as exc:
        logger.warning("expand_query failed, using original query: %s", exc)
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

    # ── keyword-triggered doc injection ──────────────────────────────────────
    # For queries that match known vocabulary-mismatch patterns, fetch the top
    # chunks from the mapped doc directly (bypassing the reranker score floor)
    # and inject them into the result set before final re-sort.
    for pattern, pinned_doc_id in _KEYWORD_DOC_MAP:
        if pattern.search(query):
            logger.info("retrieve: keyword inject from doc_id=%s", pinned_doc_id)
            pin_cursor = collection.find_and_rerank(
                {"doc_id": pinned_doc_id},
                sort={"$hybrid": {"$vectorize": query, "$lexical": query}},
                rerank_query=query, rerank_on="text",
                limit=2, hybrid_limits=5,
                projection={"$vectorize": 0},
                include_scores=True,
            )
            pin_chunks = []
            for r in pin_cursor:
                doc = dict(r.document)
                doc["rerank_score"] = (r.scores or {}).get("$rerank", 0.0)
                doc["rrf_score"]    = (r.scores or {}).get("$rrf", 0.0)
                pin_chunks.append(doc)
            if pin_chunks:
                seen_pin: dict[str, dict] = {c["_id"]: c for c in chunks}
                for c in pin_chunks:
                    if c["_id"] not in seen_pin:
                        seen_pin[c["_id"]] = c
                # keep pinned chunks — re-sort everything by rerank_score,
                # but give pinned chunks a floor score so they aren't last
                max_score = max((c["rerank_score"] for c in seen_pin.values()), default=0.0)
                for c in pin_chunks:
                    if c["_id"] in seen_pin:
                        seen_pin[c["_id"]]["rerank_score"] = max(
                            c["rerank_score"], max_score * 0.5
                        )
                chunks = sorted(seen_pin.values(),
                                key=lambda x: x["rerank_score"], reverse=True)[:top_k]
                logger.info("retrieve: injected %d pinned chunks", len(pin_chunks))

    # ── query expansion: also search with synonym-expanded query ─────────────
    # Runs for all queries (EN and ZH) to bridge vocabulary mismatches between
    # user language and SFC/PCPD document terminology (e.g. "internal audit"
    # vs "review function"). Merges with primary results, deduplicating by _id.
    expanded_query = _expand_query(query)
    if expanded_query != query:
        exp_chunks = _run_search(expanded_query)
        seen_exp: dict[str, dict] = {c["_id"]: c for c in chunks}
        for c in exp_chunks:
            cid = c["_id"]
            if cid not in seen_exp or c["rerank_score"] > seen_exp[cid]["rerank_score"]:
                seen_exp[cid] = c
        chunks = sorted(seen_exp.values(), key=lambda x: x["rerank_score"], reverse=True)[:top_k]
        logger.info("retrieve: merged %d expanded chunks → %d total", len(exp_chunks), len(chunks))

    logger.info(
        "retrieve: query=%r  source=%s  returned=%d",
        query[:60],
        source_filter or "ALL",
        len(chunks),
    )
    return chunks
