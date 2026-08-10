"""Hybrid retriever for the RAG pipeline.

Combines semantic (ANN vector) search and keyword (text) search against the
AstraDB ``chunks`` collection, then merges the two ranked lists using
Reciprocal Rank Fusion (RRF) before returning the top-K chunks.

Why hybrid?
-----------
Regulatory documents contain formal legal language (clause numbers, defined
terms) that keyword search handles well, *and* conceptual questions
("what are the requirements for client asset segregation?") that semantic
search handles well.  RRF consistently outperforms either alone.

Required environment variables
-------------------------------
ASTRA_DB_APPLICATION_TOKEN
ASTRA_DB_API_ENDPOINT
ASTRA_DB_KEYSPACE            (optional, default "default_keyspace")
WATSONX_API_KEY
WATSONX_PROJECT_ID
WATSONX_URL
WATSONX_EMBED_MODEL
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from astrapy import DataAPIClient

from core.embedder import embed_texts

logger = logging.getLogger(__name__)

CHUNKS_COLLECTION = "chunks"

# RRF constant — standard value; higher k = less sensitive to rank position
_RRF_K = 60


def _get_collection():
    token    = os.environ["ASTRA_DB_APPLICATION_TOKEN"]
    endpoint = os.environ["ASTRA_DB_API_ENDPOINT"].rstrip("/")
    keyspace = os.environ.get("ASTRA_DB_KEYSPACE", "default_keyspace")
    client   = DataAPIClient(token)
    database = client.get_database(endpoint, keyspace=keyspace)
    return database.get_collection(CHUNKS_COLLECTION)


def _rrf_merge(
    semantic_hits: list[dict],
    keyword_hits: list[dict],
    k: int = _RRF_K,
) -> list[dict]:
    """Merge two ranked lists with Reciprocal Rank Fusion.

    Each hit must have an ``_id`` field.  The merged list is sorted by
    descending RRF score.  Hits present in both lists get a higher score.
    The returned dicts are from *semantic_hits* when a doc appears in both
    (keyword hits carry less metadata).
    """
    scores: dict[str, float] = {}
    docs:   dict[str, dict]  = {}

    for rank, hit in enumerate(semantic_hits):
        _id = hit["_id"]
        scores[_id]  = scores.get(_id, 0.0) + 1.0 / (k + rank + 1)
        docs[_id]    = hit

    for rank, hit in enumerate(keyword_hits):
        _id = hit["_id"]
        scores[_id]  = scores.get(_id, 0.0) + 1.0 / (k + rank + 1)
        if _id not in docs:
            docs[_id] = hit

    merged = sorted(scores.keys(), key=lambda i: scores[i], reverse=True)
    result = []
    for _id in merged:
        doc = dict(docs[_id])
        doc["rrf_score"] = scores[_id]
        result.append(doc)
    return result


def retrieve(
    query: str,
    *,
    source_filter: Optional[str] = None,
    top_n: int = 20,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Return the top-K most relevant chunks for *query*.

    Parameters
    ----------
    query:
        User's natural-language question.
    source_filter:
        Optional source restriction: ``"SFC"`` or ``"PCPD"``.
        Pass ``None`` to search across all sources.
    top_n:
        Number of candidates to retrieve from each of the two searches
        before merging.  More candidates → better recall but slower rerank.
    top_k:
        Final number of chunks to return after RRF merge.

    Returns
    -------
    List of chunk dicts (without ``$vector``), sorted by descending RRF score.
    Each dict has: ``_id``, ``doc_id``, ``source``, ``chunk_index``,
    ``section_heading``, ``page_start``, ``text``, ``token_count``,
    ``rrf_score``.
    """
    collection = _get_collection()

    # ── build optional source pre-filter ─────────────────────────────────────
    filter_doc: dict = {}
    if source_filter:
        filter_doc["source"] = source_filter.upper()

    # ── 1. semantic (ANN vector) search ───────────────────────────────────────
    query_vector = embed_texts([query])[0]

    semantic_cursor = collection.find(
        filter_doc,
        sort={"$vector": query_vector},
        limit=top_n,
        projection={"$vector": 0},  # don't return the vector bytes
    )
    semantic_hits = list(semantic_cursor)
    logger.debug("Semantic hits: %d", len(semantic_hits))

    # ── 2. keyword (BM25 lexical) search ─────────────────────────────────────
    # AstraDB Data API v3: the lexical sort key is "$lexical" (not "$text").
    # Chunks must have been inserted with a "$lexical" field for BM25 to work;
    # see store/astra_chunk_store.py.
    keyword_cursor = collection.find(
        filter_doc,
        sort={"$lexical": query},
        limit=top_n,
        projection={"$vector": 0},
    )
    keyword_hits = list(keyword_cursor)
    logger.debug("Keyword hits: %d", len(keyword_hits))

    # ── 3. RRF merge ──────────────────────────────────────────────────────────
    merged = _rrf_merge(semantic_hits, keyword_hits)
    top_chunks = merged[:top_k]

    logger.info(
        "retrieve: query=%r  source=%s  semantic=%d  keyword=%d  merged=%d  returned=%d",
        query[:60],
        source_filter or "ALL",
        len(semantic_hits),
        len(keyword_hits),
        len(merged),
        len(top_chunks),
    )
    return top_chunks
