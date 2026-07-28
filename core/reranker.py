"""Reranker — WatsonX Rerank API with local cross-encoder fallback.

After hybrid retrieval, the top-N candidate chunks are reranked by a
cross-encoder model that jointly scores (query, passage) pairs.  This
typically yields a significant precision improvement over bi-encoder
retrieval alone.

Behaviour is controlled by the ``RERANKER`` env var:
    ``watsonx``  — IBM WatsonX Rerank API  (default)
    ``local``    — sentence-transformers cross-encoder (free, no API call)

Required environment variables (WatsonX mode)
----------------------------------------------
WATSONX_API_KEY
WATSONX_PROJECT_ID
WATSONX_URL
RERANKER_MODEL    WatsonX rerank model ID
                  (defaults to "cross-encoder/ms-marco-minilm-l-12-v2"
                   — check your WatsonX plan for available rerank models)

Required packages (local mode only — not in requirements.txt by default)
------------------------------------------------------------------------
    pip install sentence-transformers
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

# WatsonX rerank endpoint path
_RERANK_PATH = "/ml/v1/text/rerank?version=2023-10-25"

# Default WatsonX rerank model
_DEFAULT_WX_RERANK_MODEL = "cross-encoder/ms-marco-minilm-l-12-v2"

# Default local cross-encoder model (sentence-transformers)
_DEFAULT_LOCAL_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# IAM token — reuse the cache from embedder if already fetched
from core.embedder import _get_iam_token  # noqa: E402 (internal reuse)


def _rerank_watsonx(
    query: str,
    chunks: list[dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    """Rerank *chunks* using the WatsonX Rerank API."""
    api_key    = os.environ["WATSONX_API_KEY"]
    project_id = os.environ["WATSONX_PROJECT_ID"]
    base_url   = os.environ["WATSONX_URL"].rstrip("/")
    model_id   = os.environ.get("RERANKER_MODEL", _DEFAULT_WX_RERANK_MODEL)

    token = _get_iam_token(api_key)
    url   = base_url + _RERANK_PATH

    inputs = [{"text": c["text"]} for c in chunks]

    resp = requests.post(
        url,
        json={
            "model_id":   model_id,
            "project_id": project_id,
            "query":      query,
            "inputs":     inputs,
            "parameters": {"top_n": top_k, "return_options": {"inputs": False}},
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"WatsonX rerank failed HTTP {resp.status_code}: {resp.text[:300]}"
        )

    results = resp.json().get("results", [])
    reranked = []
    for item in results:
        idx   = item["index"]
        score = item["score"]
        chunk = dict(chunks[idx])
        chunk["rerank_score"] = score
        reranked.append(chunk)

    return reranked[:top_k]


def _rerank_local(
    query: str,
    chunks: list[dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    """Rerank *chunks* using a local sentence-transformers cross-encoder."""
    try:
        from sentence_transformers import CrossEncoder  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "sentence-transformers is required for RERANKER=local. "
            "Install it with: pip install sentence-transformers"
        ) from exc

    model_name = os.environ.get("RERANKER_MODEL", _DEFAULT_LOCAL_MODEL)
    model = CrossEncoder(model_name)

    pairs  = [(query, c["text"]) for c in chunks]
    scores = model.predict(pairs)

    scored = sorted(
        zip(scores, chunks),
        key=lambda x: x[0],
        reverse=True,
    )
    reranked = []
    for score, chunk in scored[:top_k]:
        c = dict(chunk)
        c["rerank_score"] = float(score)
        reranked.append(c)
    return reranked


def rerank(
    query: str,
    chunks: list[dict[str, Any]],
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Rerank *chunks* and return the top-K most relevant ones.

    Parameters
    ----------
    query:
        The user's question (same string used for retrieval).
    chunks:
        Candidate chunks as returned by ``core/retriever.retrieve()``.
    top_k:
        Number of chunks to return after reranking.

    Returns
    -------
    List of chunk dicts sorted by descending rerank score, each with an
    added ``rerank_score`` field.
    """
    if not chunks:
        return []

    backend = os.environ.get("RERANKER", "watsonx").lower()

    logger.info(
        "rerank: backend=%s  candidates=%d  top_k=%d", backend, len(chunks), top_k
    )

    if backend == "watsonx":
        return _rerank_watsonx(query, chunks, top_k)
    if backend == "local":
        return _rerank_local(query, chunks, top_k)

    raise ValueError(
        f"Unknown RERANKER value '{backend}'. Set to 'watsonx' or 'local'."
    )
