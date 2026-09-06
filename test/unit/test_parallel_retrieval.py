"""Tests for parallel SFC + PCPD retrieval in api/routers/chat.py (Phase 2).

Covers:
- _merge_chunks deduplicates by _id keeping highest rerank_score
- _merge_chunks caps result at top_k
- _merge_chunks handles empty lists
- _retrieve_candidates uses single retrieve() when source_filter is set
- _retrieve_candidates fans out two concurrent calls when source is None
- _retrieve_candidates merges results from both sources
- _retrieve_candidates returns partial results when one source fails
- _retrieve_candidates raises when both sources fail
"""

from __future__ import annotations

from unittest.mock import call, patch

import pytest

from api.routers.chat import _merge_chunks, _retrieve_candidates


# ── _merge_chunks ─────────────────────────────────────────────────────────────

def _chunk(id: str, score: float, source: str = "SFC") -> dict:
    return {
        "_id": id,
        "doc_id": f"doc-{id}",
        "source": source,
        "chunk_index": 0,
        "section_heading": "Test",
        "page_start": 1,
        "text": f"text for {id}",
        "token_count": 10,
        "rerank_score": score,
    }


def test_merge_chunks_deduplicates_keeps_highest_score():
    sfc_chunks  = [_chunk("a", 0.9), _chunk("b", 0.5)]
    pcpd_chunks = [_chunk("a", 0.3), _chunk("c", 0.7)]  # "a" duplicate, lower score

    merged = _merge_chunks([sfc_chunks, pcpd_chunks], top_k=10)
    ids = [c["_id"] for c in merged]

    assert ids.count("a") == 1                        # deduplicated
    assert next(c for c in merged if c["_id"] == "a")["rerank_score"] == 0.9  # kept higher


def test_merge_chunks_sorted_descending():
    lists = [[_chunk("a", 0.4), _chunk("b", 0.9), _chunk("c", 0.1)]]
    merged = _merge_chunks(lists, top_k=10)
    scores = [c["rerank_score"] for c in merged]
    assert scores == sorted(scores, reverse=True)


def test_merge_chunks_caps_at_top_k():
    lists = [[_chunk(str(i), float(i) / 10) for i in range(20)]]
    merged = _merge_chunks(lists, top_k=5)
    assert len(merged) == 5


def test_merge_chunks_empty_lists():
    assert _merge_chunks([], top_k=10) == []
    assert _merge_chunks([[]], top_k=10) == []
    assert _merge_chunks([[], []], top_k=10) == []


# ── _retrieve_candidates ──────────────────────────────────────────────────────

@patch("api.routers.chat.retrieve")
def test_retrieve_candidates_single_source(mock_retrieve):
    """When source is set, retrieve() is called once with that source."""
    chunks = [_chunk("a", 0.8)]
    mock_retrieve.return_value = chunks

    result = _retrieve_candidates("What is SFC?", source="SFC", retrieve_n=20, top_k=5)

    mock_retrieve.assert_called_once_with(
        "What is SFC?", source_filter="SFC", top_n=20, top_k=5
    )
    assert result == chunks


@patch("api.routers.chat.retrieve")
def test_retrieve_candidates_parallel_calls_both_sources(mock_retrieve):
    """When source is None, retrieve() is called for both SFC and PCPD."""
    mock_retrieve.return_value = []

    _retrieve_candidates("What are the requirements?", source=None, retrieve_n=20, top_k=5)

    called_sources = {c.kwargs["source_filter"] for c in mock_retrieve.call_args_list}
    assert called_sources == {"SFC", "PCPD"}


@patch("api.routers.chat.retrieve")
def test_retrieve_candidates_parallel_merges_results(mock_retrieve):
    """Parallel results from SFC and PCPD are merged and deduplicated."""
    sfc_chunks  = [_chunk("a", 0.9, "SFC"), _chunk("b", 0.5, "SFC")]
    pcpd_chunks = [_chunk("c", 0.7, "PCPD"), _chunk("b", 0.3, "PCPD")]  # "b" duplicate

    def side_effect(question, *, source_filter, top_n, top_k):
        return sfc_chunks if source_filter == "SFC" else pcpd_chunks

    mock_retrieve.side_effect = side_effect

    result = _retrieve_candidates("question", source=None, retrieve_n=20, top_k=10)

    ids = [c["_id"] for c in result]
    assert sorted(ids) == ["a", "b", "c"]             # all 3 unique chunks present
    assert ids.count("b") == 1                         # deduplicated
    b_score = next(c["rerank_score"] for c in result if c["_id"] == "b")
    assert b_score == 0.5                              # kept SFC's higher score


@patch("api.routers.chat.retrieve")
def test_retrieve_candidates_partial_failure_returns_successful_source(mock_retrieve):
    """If one source fails, results from the other source are still returned."""
    sfc_chunks = [_chunk("a", 0.8, "SFC")]

    def side_effect(question, *, source_filter, top_n, top_k):
        if source_filter == "PCPD":
            raise RuntimeError("AstraDB timeout")
        return sfc_chunks

    mock_retrieve.side_effect = side_effect

    result = _retrieve_candidates("question", source=None, retrieve_n=20, top_k=10)

    assert result == sfc_chunks   # SFC results returned despite PCPD failure


@patch("api.routers.chat.retrieve")
def test_retrieve_candidates_both_fail_raises(mock_retrieve):
    """If both SFC and PCPD fail, a RuntimeError is raised."""
    mock_retrieve.side_effect = RuntimeError("AstraDB timeout")

    with pytest.raises(RuntimeError, match="All parallel retrieval calls failed"):
        _retrieve_candidates("question", source=None, retrieve_n=20, top_k=10)
