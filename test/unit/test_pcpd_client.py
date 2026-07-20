import pytest

from core.pcpd_client import PCPDClient


def test_discover_returns_four_known_documents():
    docs = PCPDClient().discover_documents()
    assert len(docs) == 4
    for doc in docs:
        assert doc["source"] == "PCPD"
        assert doc["download_url"].startswith("https://www.pcpd.org.hk/")
        assert doc["doc_id"].startswith("pcpd-")


def test_discover_is_idempotent_across_calls():
    first = {d["doc_id"] for d in PCPDClient().discover_documents()}
    second = {d["doc_id"] for d in PCPDClient().discover_documents()}
    assert first == second


def test_discover_documents_returns_independent_copies():
    """Mutating a returned record must not corrupt later calls.

    discover_documents() currently does `list(_KNOWN_DOCUMENTS)`, which
    copies the outer list but not the record dicts — callers still share
    the same dict objects as the module-level `_KNOWN_DOCUMENTS`.
    """
    first = PCPDClient().discover_documents()
    first[0]["title"] = "TAMPERED"

    second = PCPDClient().discover_documents()
    assert second[0]["title"] != "TAMPERED"
