"""AstraDB store for RAG chunks.

Each chunk produced by ``core/chunker.py`` is stored as a document in the
AstraDB ``chunks`` collection (created by ``scripts/setup_astra_collections.py``).

Embeddings are generated automatically by AstraDB using the NVIDIA
``nvidia/nv-embedqa-e5-v5`` model configured on the collection (vectorize).
No WatsonX embedding call is needed for storage — just pass the text in the
``$vectorize`` field and AstraDB handles the rest.

Document shape in the collection
---------------------------------
{
    "_id":             "<doc_id>__c<n>",   # same as chunk_id
    "$vectorize":      str,                # text → AstraDB auto-embeds (NVIDIA)
    "$lexical":        str,                # same text for BM25 keyword search
    "doc_id":          str,                # links to COS
    "source":          str,                # "SFC" | "PCPD"
    "chunk_index":     int,
    "section_heading": str,
    "page_start":      int,
    "text":            str,                # raw text copy
    "token_count":     int,
}

Required environment variables
-------------------------------
ASTRA_DB_APPLICATION_TOKEN   AstraCS:… token
ASTRA_DB_API_ENDPOINT        https://<db-id>-<region>.apps.astra.datastax.com
ASTRA_DB_KEYSPACE            e.g. "default_keyspace"
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from astrapy import DataAPIClient

logger = logging.getLogger(__name__)

CHUNKS_COLLECTION = "chunks"


def _get_collection():
    """Return an astrapy Collection handle for the chunks collection."""
    token    = os.environ["ASTRA_DB_APPLICATION_TOKEN"]
    endpoint = os.environ["ASTRA_DB_API_ENDPOINT"].rstrip("/")
    keyspace = os.environ.get("ASTRA_DB_KEYSPACE", "default_keyspace")

    client   = DataAPIClient(token)
    database = client.get_database(endpoint, keyspace=keyspace)
    return database.get_collection(CHUNKS_COLLECTION)


class AstraChunkStore:
    """Store for RAG chunks backed by the AstraDB ``chunks`` collection.

    Call ``setup_astra_collections.py`` once before using this class to
    ensure the collection exists with the correct vector dimension.
    """

    def __init__(self) -> None:
        self._col = _get_collection()
        logger.debug("AstraChunkStore connected to collection '%s'", CHUNKS_COLLECTION)

    # ── write ─────────────────────────────────────────────────────────────────

    def upsert_chunks(
        self,
        chunks: list[dict[str, Any]],
        vectors: Optional[list[list[float]]] = None,
    ) -> None:
        """Insert or overwrite a batch of chunks.

        Embeddings are generated automatically by AstraDB via the
        ``$vectorize`` field (NVIDIA nvidia/nv-embedqa-e5-v5 model).
        The ``vectors`` parameter is accepted for backwards compatibility
        but ignored — AstraDB handles embedding internally.

        Parameters
        ----------
        chunks:
            List of chunk dicts as returned by ``core/chunker.py``.
            Each must contain at minimum: ``chunk_id``, ``doc_id``,
            ``source``, ``chunk_index``, ``section_heading``, ``text``,
            ``token_count``.  ``page_start`` is optional (defaults to 0).
        vectors:
            Ignored. Kept for backwards compatibility only.
        """
        for chunk in chunks:
            doc = {
                "_id":             chunk["chunk_id"],
                # $vectorize: AstraDB auto-embeds this text using the
                # collection's configured NVIDIA embedding model.
                "$vectorize":      chunk["text"],
                # $lexical: same text for BM25 keyword search (EN + CJK).
                "$lexical":        chunk["text"],
                "doc_id":          chunk["doc_id"],
                "source":          chunk.get("source", ""),
                "chunk_index":     chunk["chunk_index"],
                "section_heading": chunk.get("section_heading", ""),
                "page_start":      chunk.get("page_start", 0),
                "text":            chunk["text"],
                "token_count":     chunk.get("token_count", 0),
            }
            # replace_one with upsert=True inserts if absent, replaces if present
            self._col.replace_one({"_id": doc["_id"]}, doc, upsert=True)

        logger.info(
            "Upserted %d chunks for doc_id=%s",
            len(chunks),
            chunks[0]["doc_id"] if chunks else "?",
        )

    # ── read ──────────────────────────────────────────────────────────────────

    def chunks_exist(self, doc_id: str) -> bool:
        """Return True if at least one chunk for *doc_id* is already stored."""
        doc = self._col.find_one({"doc_id": doc_id}, projection={"_id": 1})
        return doc is not None

    def get_chunks(self, doc_id: str) -> list[dict[str, Any]]:
        """Return all stored chunks for *doc_id* ordered by chunk_index."""
        cursor = self._col.find(
            {"doc_id": doc_id},
            projection={"$vector": 0},  # omit vector — not needed for display
            sort={"chunk_index": 1},
        )
        return list(cursor)

    # ── delete ────────────────────────────────────────────────────────────────

    def delete_chunks(self, doc_id: str) -> None:
        """Delete all chunks for *doc_id*."""
        result = self._col.delete_many({"doc_id": doc_id})
        logger.info(
            "Deleted chunks for doc_id=%s (deleted_count=%s)",
            doc_id,
            result.deleted_count,
        )
