#!/usr/bin/env python
"""One-time setup: create all AstraDB collections for the RAG pipeline.

Safe to re-run — skips creation if the collection already exists.

Collection created
------------------
chunks      One document per text chunk from a regulatory PDF.

            Uses AstraDB vectorize with the NVIDIA
            ``nvidia/nv-embedqa-e5-v5`` embedding model (1024-dim, COSINE).
            Text passed in the ``$vectorize`` field is auto-embedded by
            AstraDB — no WatsonX embedding call is needed.

            BM25 keyword search is enabled via the ``$lexical`` field,
            which is populated alongside ``$vectorize`` on every insert.

            Reranking is performed in-place by ``find_and_rerank()`` using
            the collection's built-in NVIDIA reranker
            (nvidia/llama-3.2-nv-rerankqa-1b-v2).

Usage
-----
    .venv/bin/python3 scripts/setup_astra_collections.py

Required env vars (.env)
------------------------
ASTRA_DB_APPLICATION_TOKEN    AstraCS:…
ASTRA_DB_API_ENDPOINT         https://<db-id>-<region>.apps.astra.datastax.com
ASTRA_DB_KEYSPACE             regulatory   (optional, defaults to default_keyspace)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# ── load .env ─────────────────────────────────────────────────────────────────
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("setup_astra")

# ── env-var guard ─────────────────────────────────────────────────────────────
REQUIRED = ["ASTRA_DB_APPLICATION_TOKEN", "ASTRA_DB_API_ENDPOINT"]
missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    log.error("Missing env vars: %s", ", ".join(missing))
    log.error("Populate your .env and re-run.")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astrapy import DataAPIClient                                             # noqa: E402
from astrapy.constants import VectorMetric                                    # noqa: E402
from astrapy.info import (                                                    # noqa: E402
    CollectionDefinition,
    CollectionVectorOptions,
    VectorServiceOptions,
)

# ── configuration ─────────────────────────────────────────────────────────────

CHUNKS_COLLECTION = "chunks"

# NVIDIA nv-embedqa-e5-v5 produces 1024-dim vectors.
EMBEDDING_DIMENSION = 1024

# COSINE is the standard metric for normalised text embeddings.
VECTOR_METRIC = VectorMetric.COSINE

# AstraDB vectorize provider and model for auto-embedding.
# AstraDB calls NVIDIA internally — no API key required on our side.
VECTORIZE_PROVIDER = "nvidia"
VECTORIZE_MODEL    = "nvidia/nv-embedqa-e5-v5"


def main() -> None:
    token    = os.environ["ASTRA_DB_APPLICATION_TOKEN"]
    endpoint = os.environ["ASTRA_DB_API_ENDPOINT"].rstrip("/")
    keyspace = os.environ.get("ASTRA_DB_KEYSPACE", "default_keyspace")

    log.info("Connecting to AstraDB ...")
    log.info("  endpoint : %s", endpoint)
    log.info("  keyspace : %s", keyspace)

    client   = DataAPIClient(token)
    database = client.get_database(endpoint, keyspace=keyspace)
    log.info("Connected. Database: %s", database.info().name)

    # ── create 'chunks' collection ────────────────────────────────────────────
    existing = [c.name for c in database.list_collections()]
    if CHUNKS_COLLECTION in existing:
        log.info("Collection '%s' already exists — skipping creation.", CHUNKS_COLLECTION)
        collection = database.get_collection(CHUNKS_COLLECTION)
    else:
        log.info(
            "Creating collection '%s'  (dimension=%d  metric=COSINE  vectorize=%s) ...",
            CHUNKS_COLLECTION, EMBEDDING_DIMENSION, VECTORIZE_MODEL,
        )
        collection = database.create_collection(
            CHUNKS_COLLECTION,
            definition=CollectionDefinition(
                vector=CollectionVectorOptions(
                    dimension=EMBEDDING_DIMENSION,
                    metric=VECTOR_METRIC,
                    service=VectorServiceOptions(
                        provider=VECTORIZE_PROVIDER,
                        model_name=VECTORIZE_MODEL,
                    ),
                ),
            ),
        )

    log.info("Collection ready: %s", collection.full_name)

    # ── verify ────────────────────────────────────────────────────────────────
    names = [c.name for c in database.list_collections()]
    log.info("Collections in keyspace '%s': %s", keyspace, names)

    if CHUNKS_COLLECTION not in names:
        log.error(
            "'%s' not found after creation — check token permissions / keyspace.",
            CHUNKS_COLLECTION,
        )
        sys.exit(2)

    log.info("=" * 60)
    log.info("Setup complete. 'chunks' collection is ready.")
    log.info("")
    log.info("Next steps:")
    log.info("  1. Run: .venv/bin/python3 scripts/run_ocr.py")
    log.info("  2. Run: .venv/bin/python3 scripts/run_chunk.py")


if __name__ == "__main__":
    main()
