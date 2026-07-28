#!/usr/bin/env python
"""One-time setup: create all AstraDB collections for the RAG pipeline.

Safe to re-run — uses check_exists=True so it will not error on an
existing collection.

Collection created
------------------
chunks      One document per text chunk from a regulatory PDF.
            Stores a 1536-dim $vector produced by WatsonX.
            WE generate the embeddings — AstraDB only stores and searches
            them.  The AstraDB template 'service / providerKey' block is
            intentionally omitted — that is only for AstraDB-managed
            providers (OpenAI etc.) which we do NOT use.

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

# ── load .env (same pattern as run_ocr.py) ────────────────────────────────────
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

from astrapy import DataAPIClient                                       # noqa: E402
from astrapy.constants import VectorMetric                              # noqa: E402
from astrapy.info import CollectionDefinition, CollectionVectorOptions  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Configuration — keep CHUNKS_COLLECTION and EMBEDDING_DIMENSION in sync with
# store/astra_chunk_store.py (written next).
# ─────────────────────────────────────────────────────────────────────────────

# Name of the collection that will hold RAG chunks.
CHUNKS_COLLECTION = "chunks"

# Must match the output size of your WatsonX embedding model.
#   ibm/slate-125m-english-rtrvr        →  768
#   ibm/slate-30m-english-rtrvr-v2      → 1536  ← your current choice
# If you change model later, delete and recreate the collection.
EMBEDDING_DIMENSION = 1536

# COSINE is correct for normalised text embeddings.
# The AstraDB template uses DOT_PRODUCT — that only works correctly when
# every vector is unit-length (explicitly L2-normalised before insert).
# COSINE handles non-unit vectors safely and is the standard choice for RAG.
VECTOR_METRIC = VectorMetric.COSINE


def main() -> None:
    token    = os.environ["ASTRA_DB_APPLICATION_TOKEN"]
    endpoint = os.environ["ASTRA_DB_API_ENDPOINT"].rstrip("/")
    keyspace = os.environ.get("ASTRA_DB_KEYSPACE", "default_keyspace")

    log.info("Connecting to AstraDB ...")
    log.info("  endpoint : %s", endpoint)
    log.info("  keyspace : %s", keyspace)

    # ── connect via astrapy Data API ──────────────────────────────────────────
    client   = DataAPIClient(token)
    database = client.get_database(endpoint, keyspace=keyspace)
    log.info("Connected. Database: %s", database.info().name)

    # ── create 'chunks' collection ────────────────────────────────────────────
    # NO service= / VectorServiceOptions block.
    # We call WatsonX ourselves to produce float vectors, then push them here.
    existing = [c.name for c in database.list_collections()]
    if CHUNKS_COLLECTION in existing:
        log.info("Collection '%s' already exists — skipping creation.", CHUNKS_COLLECTION)
        collection = database.get_collection(CHUNKS_COLLECTION)
    else:
        log.info(
            "Creating collection '%s'  (dimension=%d  metric=COSINE) ...",
            CHUNKS_COLLECTION, EMBEDDING_DIMENSION,
        )
        collection = database.create_collection(
            CHUNKS_COLLECTION,
            definition=CollectionDefinition(
                vector=CollectionVectorOptions(
                    dimension=EMBEDDING_DIMENSION,
                    metric=VECTOR_METRIC,
                    # ← No 'service' key — WatsonX generates embeddings; we push them.
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
    log.info("  1. Confirm WATSONX_EMBED_MODEL in .env")
    log.info("     → ibm/slate-30m-english-rtrvr-v2  (1536-dim)")
    log.info("  2. Run: .venv/bin/python3 scripts/run_ocr.py")


if __name__ == "__main__":
    main()
