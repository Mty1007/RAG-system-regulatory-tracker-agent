from __future__ import annotations

import os

from core.ia_client import IAClient
from core.pcpd_client import PCPDClient
from core.sfc_client import SFCClient
from store.document_store import DocumentStore

_store = None


def init_services() -> None:
    global _store
    if os.environ.get("USE_COS"):
        from store.cos_document_store import COSDocumentStore  # lazy import
        _store = COSDocumentStore()
    else:
        _store = DocumentStore()


def get_document_store():
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
