"""WatsonX embedding client.

Calls the IBM WatsonX ``/ml/v1/text/embeddings`` REST endpoint to produce
float vectors for a list of text strings.  Requests are batched (up to
``BATCH_SIZE`` texts per call) to stay within API limits.

Required environment variables
-------------------------------
WATSONX_API_KEY       IBM Cloud IAM API key
WATSONX_PROJECT_ID    WatsonX project ID
WATSONX_URL           Regional endpoint, e.g. https://us-south.ml.cloud.ibm.com
WATSONX_EMBED_MODEL   Model ID, e.g. ibm/slate-30m-english-rtrvr-v2

The IAM token is fetched once per process and cached for 50 minutes
(tokens are valid for 60 minutes; the 10-minute buffer avoids expiry
mid-batch).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# WatsonX embedding endpoint (relative to WATSONX_URL)
_EMBED_PATH = "/ml/v1/text/embeddings?version=2023-10-25"

# IAM token endpoint
_IAM_URL = "https://iam.cloud.ibm.com/identity/token"

# Maximum texts per single API call.
# ibm/granite-embedding-278m-multilingual enforces its 512-token limit
# across the entire batch payload, not per individual text.  Sending one
# text at a time is the only reliable way to stay under the limit.
BATCH_SIZE = 1

# All current WatsonX embedding models have a 512-token hard limit.
# ibm/granite-embedding-278m-multilingual tokenises aggressively:
#   - dot-leaders ("......1")  → 3–5 tokens per word
#   - CJK characters           → 2–4 tokens per character (counted as 1 word)
# _MAX_WORDS=100 gives ~400 tokens worst-case for mixed EN/ZH regulatory text.
_MAX_WORDS = 100

# Token cache: (access_token, expiry_epoch)
_token_cache: tuple[str, float] = ("", 0.0)


def _get_iam_token(api_key: str) -> str:
    """Fetch (or return cached) IBM IAM access token."""
    global _token_cache
    token, expiry = _token_cache
    if token and time.time() < expiry:
        return token

    resp = requests.post(
        _IAM_URL,
        data={
            "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
            "apikey": api_key,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"IAM token request failed HTTP {resp.status_code}: {resp.text[:200]}"
        )
    payload = resp.json()
    token = payload["access_token"]
    # expires_in is in seconds; cache with a 10-minute safety buffer
    expiry = time.time() + int(payload.get("expires_in", 3600)) - 600
    _token_cache = (token, expiry)
    logger.debug("IAM token refreshed")
    return token


def embed_texts(
    texts: list[str],
    *,
    api_key: Optional[str] = None,
    project_id: Optional[str] = None,
    base_url: Optional[str] = None,
    model_id: Optional[str] = None,
) -> list[list[float]]:
    """Embed a list of strings using WatsonX and return a list of float vectors.

    Parameters are read from environment variables if not supplied directly.

    Parameters
    ----------
    texts:
        Non-empty list of strings to embed.
    api_key, project_id, base_url, model_id:
        Override the corresponding environment variable (useful in tests).

    Returns
    -------
    List of float vectors, one per input text, in the same order.
    """
    if not texts:
        return []

    _api_key    = api_key    or os.environ["WATSONX_API_KEY"]
    _project_id = project_id or os.environ["WATSONX_PROJECT_ID"]
    _base_url   = (base_url  or os.environ["WATSONX_URL"]).rstrip("/")
    _model_id   = model_id   or os.environ["WATSONX_EMBED_MODEL"]

    token = _get_iam_token(_api_key)
    url   = _base_url + _EMBED_PATH

    # Normalise and truncate each text before embedding:
    # 1. Collapse dot-leaders (e.g. "........1" → " 1") — these appear in
    #    tables of contents and tokenise at 3–5 tokens per dot sequence.
    # 2. Hard-truncate by character count — more reliable than word count for
    #    mixed English/Chinese text where a single CJK "word" can be 10+ tokens.
    #    600 chars ≈ 400 tokens for worst-case dense Chinese regulatory text.
    import re as _re
    _dot_leader = _re.compile(r'\.{3,}')
    _MAX_CHARS = 600

    def _truncate(text: str) -> str:
        text = _dot_leader.sub(' ', text)
        return text[:_MAX_CHARS] if len(text) > _MAX_CHARS else text

    texts = [_truncate(t) for t in texts]

    vectors: list[list[float]] = []

    for batch_start in range(0, len(texts), BATCH_SIZE):
        batch = texts[batch_start : batch_start + BATCH_SIZE]
        resp = requests.post(
            url,
            json={
                "model_id": _model_id,
                "project_id": _project_id,
                "inputs": batch,
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=120,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"WatsonX embed failed HTTP {resp.status_code}: {resp.text[:300]}"
            )
        results = resp.json().get("results", [])
        for item in results:
            vectors.append(item["embedding"])

    if len(vectors) != len(texts):
        raise RuntimeError(
            f"Embedding count mismatch: expected {len(texts)}, got {len(vectors)}"
        )

    return vectors
