from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


class DocumentStore:
    """In-memory document store — zero setup required.

    Same shape as a real backing store's documents collection would need:
    get by id, insert, list. Swap this for a real database later without
    changing anything in api/ or core/ — they only depend on this interface.
    """

    def __init__(self):
        self._docs: dict[str, dict[str, Any]] = {}

    def get_document(self, doc_id: str) -> Optional[dict[str, Any]]:
        return self._docs.get(doc_id)

    def insert_document(self, record: dict[str, Any]) -> None:
        record.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        self._docs[record["doc_id"]] = record

    def list_documents(self, source: Optional[str] = None) -> list[dict[str, Any]]:
        docs = list(self._docs.values())
        if source:
            docs = [d for d in docs if d.get("source") == source]
        return docs
