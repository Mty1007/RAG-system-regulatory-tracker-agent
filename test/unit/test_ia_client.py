import pytest

from core.ia_client import IAClient


def test_discover_2026_count():
    docs = IAClient().discover_documents(start_year=2026, end_year=2026)
    assert len(docs) == 18


def test_discover_2025_count():
    docs = IAClient().discover_documents(start_year=2025, end_year=2025)
    assert len(docs) == 42
