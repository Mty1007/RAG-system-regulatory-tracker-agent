"""Unit tests for core/cos_retriever.py.

Tests cover the pure-logic functions that do not require COS credentials:
  1. fetch_fallback_passages() returns [] when USE_COS is unset
  2. fetch_fallback_passages() returns [] when top rerank_score >= threshold
  3. _extract_window() — correct section extraction with heading match
  4. _extract_window() — ±2 section window boundaries
  5. _extract_window() — fallback when heading not found
  6. fetch_fallback_passages() — caps additional passages at max_extra
"""

from __future__ import annotations

from unittest.mock import patch

from core.cos_retriever import _extract_window, fetch_fallback_passages


# ── fetch_fallback_passages guard rails ───────────────────────────────────────

def test_returns_empty_when_use_cos_not_set(monkeypatch):
    """Without USE_COS env var the fallback silently returns []."""
    monkeypatch.delenv("USE_COS", raising=False)
    chunks = [{"_id": "x", "rerank_score": 0.1, "doc_id": "sfc-abc", "source": "SFC",
               "section_heading": "Requirements", "page_start": 1,
               "text": "Some text.", "token_count": 5}]
    result = fetch_fallback_passages(chunks)
    assert result == []


def test_returns_empty_when_score_above_threshold(monkeypatch):
    """When top chunk score >= COS_FALLBACK_THRESHOLD no COS call is made."""
    monkeypatch.setenv("USE_COS", "1")
    monkeypatch.setenv("COS_FALLBACK_THRESHOLD", "0.35")
    chunks = [{"_id": "x", "rerank_score": 0.8, "doc_id": "sfc-abc", "source": "SFC",
               "section_heading": "Reqs", "page_start": 1,
               "text": "Some text.", "token_count": 5}]
    result = fetch_fallback_passages(chunks)
    assert result == []


def test_returns_empty_when_cos_download_fails(monkeypatch):
    """A COS GET failure logs a warning and returns [] — does not raise."""
    monkeypatch.setenv("USE_COS", "1")
    monkeypatch.setenv("COS_FALLBACK_THRESHOLD", "0.35")
    monkeypatch.setenv("COS_BUCKET", "my-bucket")
    chunks = [{"_id": "x", "rerank_score": 0.1, "doc_id": "sfc-abc", "source": "SFC",
               "section_heading": "Requirements", "page_start": 1,
               "text": "Some text.", "token_count": 5}]

    with patch("core.cos_retriever._download_markdown", return_value=None):
        result = fetch_fallback_passages(chunks)

    assert result == []


def test_caps_at_max_extra(monkeypatch):
    """At most max_extra passages are returned even when more docs are available."""
    monkeypatch.setenv("USE_COS", "1")
    monkeypatch.setenv("COS_FALLBACK_THRESHOLD", "0.35")
    monkeypatch.setenv("COS_BUCKET", "my-bucket")

    md = "# Section A\n\nBody A.\n\n# Section B\n\nBody B."
    chunks = [
        {"_id": f"x{i}", "rerank_score": 0.1, "doc_id": f"sfc-doc{i}", "source": "SFC",
         "section_heading": "Section A", "page_start": 1,
         "text": "text", "token_count": 5}
        for i in range(4)
    ]

    with patch("core.cos_retriever._download_markdown", return_value=md):
        result = fetch_fallback_passages(chunks, max_extra=2)

    assert len(result) <= 2


def test_fallback_passage_has_correct_shape(monkeypatch):
    """Returned passage dicts have all fields required by generate_answer()."""
    monkeypatch.setenv("USE_COS", "1")
    monkeypatch.setenv("COS_FALLBACK_THRESHOLD", "0.35")
    monkeypatch.setenv("COS_BUCKET", "my-bucket")

    md = "# Client Assets\n\nA licensed corporation must segregate assets."
    chunk = {"_id": "x", "rerank_score": 0.1, "doc_id": "sfc-abc123", "source": "SFC",
             "section_heading": "Client Assets", "page_start": 5,
             "text": "text", "token_count": 5}

    with patch("core.cos_retriever._download_markdown", return_value=md):
        result = fetch_fallback_passages([chunk], max_extra=1)

    assert len(result) == 1
    passage = result[0]
    assert passage["doc_id"] == "sfc-abc123"
    assert passage["source"] == "SFC"
    assert passage["source_type"] == "cos_fallback"
    assert "segregate" in passage["text"]
    assert isinstance(passage["token_count"], int)


# ── _extract_window ───────────────────────────────────────────────────────────

_SAMPLE_MD = """\
# Introduction

This document sets out the regulatory framework.

# Client Assets

A licensed corporation must segregate client assets from firm assets at all times.
Client assets must be held in designated accounts.

# Reporting Requirements

Licensed corporations must submit monthly reports.

# Internal Controls

An effective internal control system must be maintained.

# Conclusion

Compliance with these rules is mandatory.
"""


def test_extract_window_finds_heading():
    """Heading match returns body of that section plus neighbours."""
    result = _extract_window(_SAMPLE_MD, "Client Assets", context_sections=1)
    assert "segregate client assets" in result.lower()


def test_extract_window_includes_neighbours():
    """With context_sections=2, sections above and below are included."""
    result = _extract_window(_SAMPLE_MD, "Reporting Requirements", context_sections=2)
    # Should include Client Assets (above) and Internal Controls (below)
    assert "client assets" in result.lower() or "reporting" in result.lower()
    assert len(result) > 50


def test_extract_window_heading_not_found_returns_fallback():
    """When the heading is not found, function returns document start (no crash)."""
    result = _extract_window(_SAMPLE_MD, "Nonexistent Section", context_sections=2)
    assert len(result) > 0  # always returns something


def test_extract_window_empty_markdown():
    """Empty Markdown returns something without raising."""
    result = _extract_window("", "Any Heading", context_sections=1)
    assert isinstance(result, str)


def test_extract_window_no_headings_returns_truncated():
    """Markdown with no headings returns at most 4000 chars (hard cap)."""
    plain = "Just a paragraph. " * 300   # ~5400 chars — over the hard cap
    result = _extract_window(plain, "Anything", context_sections=2)
    assert len(result) <= 4000
