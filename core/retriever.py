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

Required environment variables
-------------------------------
ASTRA_DB_APPLICATION_TOKEN
ASTRA_DB_API_ENDPOINT
ASTRA_DB_KEYSPACE            (optional, default "default_keyspace")
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from astrapy import DataAPIClient

logger = logging.getLogger(__name__)

CHUNKS_COLLECTION = "chunks"


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
    top_k: int = 5,
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

    # ── hybrid retrieval + reranking in one call ──────────────────────────────
    # $vectorize: AstraDB auto-embeds the query text using the collection's
    # configured NVIDIA model — no WatsonX embed call needed.
    # $hybrid sort combines $vectorize (ANN semantic) + $lexical (BM25 keyword).
    # rerank_on="text" tells the NVIDIA reranker which field to score against.
    cursor = collection.find_and_rerank(
        filter_doc,
        sort={"$hybrid": {"$vectorize": query, "$lexical": query}},
        rerank_query=query,
        rerank_on="text",
        limit=top_k,
        hybrid_limits=top_n,
        projection={"$vectorize": 0},
        include_scores=True,
    )

    results = list(cursor)

    chunks = []
    for r in results:
        doc = dict(r.document)
        scores = r.scores or {}
        doc["rerank_score"] = scores.get("$rerank", 0.0)
        doc["rrf_score"]    = scores.get("$rrf", 0.0)
        chunks.append(doc)

    logger.info(
        "retrieve: query=%r  source=%s  returned=%d",
        query[:60],
        source_filter or "ALL",
        len(chunks),
    )
    return chunks
