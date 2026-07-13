from __future__ import annotations

from core.ia_client import IAClient
from core.pcpd_client import PCPDClient
from core.sfc_client import SFCClient
from store.document_store import DocumentStore

_store: DocumentStore | None = None


def init_services() -> None:
    global _store
    _store = DocumentStore()


def get_document_store() -> DocumentStore:
    assert _store is not None, "DocumentStore not initialised"
    return _store


def get_regulator_client(source: str):
    if source == "SFC":
        return SFCClient()
    if source == "IA":
        return IAClient()
    if source == "PCPD":
        return PCPDClient()
    raise ValueError(f"Unknown source: {source}")
