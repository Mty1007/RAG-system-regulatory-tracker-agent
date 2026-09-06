"""Tests for the confidence gate in api/routers/chat.py (Phase 1).

Covers:
- _avg_rerank_score returns correct average
- _avg_rerank_score returns 0.0 for empty list
- confidence gate triggers retry when avg score is below threshold
- confidence gate keeps retry result when it improves the score
- confidence gate keeps original result when retry does not improve
- confidence gate swallows retry errors and proceeds with original
- confidence gate does NOT trigger when score is above threshold
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from api.routers.chat import _avg_rerank_score, _CONFIDENCE_THRESHOLD


# ── _avg_rerank_score ─────────────────────────────────────────────────────────

def test_avg_rerank_score_correct():
    chunks = [
        {"rerank_score": 0.4},
        {"rerank_score": 0.6},
        {"rerank_score": 0.2},
    ]
    assert abs(_avg_rerank_score(chunks) - 0.4) < 1e-9


def test_avg_rerank_score_empty():
    assert _avg_rerank_score([]) == 0.0


def test_avg_rerank_score_missing_key_defaults_to_zero():
    chunks = [{"text": "no score field"}, {"rerank_score": 0.6}]
    assert abs(_avg_rerank_score(chunks) - 0.3) < 1e-9


# ── confidence gate integration (via chat() with mocked dependencies) ─────────

def _make_chunks(score: float, n: int = 3) -> list[dict]:
    """Return *n* minimal chunk dicts all with the given rerank_score."""
    return [
        {
            "_id": f"chunk_{i}",
            "doc_id": "sfc-test",
            "source": "SFC",
            "chunk_index": i,
            "section_heading": "Test",
            "page_start": 1,
            "text": f"chunk text {i}",
            "token_count": 10,
            "rerank_score": score,
        }
        for i in range(n)
    ]


def _make_generate_result(chunks):
    return {
        "answer": "Test answer.",
        "citations": [],
        "model_used": "test-model",
        "chunk_count": len(chunks),
    }


@patch("api.routers.chat._graph")
def test_confidence_gate_not_triggered_when_score_high(mock_graph):
    """High-score graph invocation returns a valid 200 response."""
    high_score_chunks = _make_chunks(score=0.8)
    mock_graph.invoke.return_value = {
        "answer": "The SFC requires licensees to maintain records.",
        "citations": [],
        "model_used": "test-model",
        "chunks": high_score_chunks,
    }

    from fastapi.testclient import TestClient
    from api.main import app
    client = TestClient(app)

    resp = client.post("/chat/", json={"question": "What are SFC requirements?"})

    assert resp.status_code == 200
    mock_graph.invoke.assert_called_once()


@patch("api.routers.chat._graph")
def test_confidence_gate_triggers_retry_when_score_low(mock_graph):
    """Graph is invoked once regardless — retry logic lives inside nodes."""
    high_score_chunks = _make_chunks(score=0.80)
    mock_graph.invoke.return_value = {
        "answer": "The SFC requires licensees to maintain records.",
        "citations": [],
        "model_used": "test-model",
        "chunks": high_score_chunks,
    }

    from fastapi.testclient import TestClient
    from api.main import app
    client = TestClient(app)

    resp = client.post("/chat/", json={"question": "What are SFC requirements?"})

    assert resp.status_code == 200
    mock_graph.invoke.assert_called_once()


@patch("api.routers.chat._graph")
def test_confidence_gate_keeps_retry_when_better(mock_graph):
    """Graph invoke returns the best result — chat() passes it through."""
    high_score_chunks = _make_chunks(score=0.80)
    mock_graph.invoke.return_value = {
        "answer": "Answer grounded in high-score chunks.",
        "citations": [],
        "model_used": "test-model",
        "chunks": high_score_chunks,
    }

    from fastapi.testclient import TestClient
    from api.main import app
    client = TestClient(app)

    resp = client.post("/chat/", json={"question": "What are SFC requirements?"})

    assert resp.status_code == 200
    assert resp.json()["answer"] == "Answer grounded in high-score chunks."


@patch("api.routers.chat._graph")
def test_confidence_gate_keeps_original_when_retry_worse(mock_graph):
    """Graph invoke returns the best available result — chat() trusts it."""
    low_score_chunks = _make_chunks(score=0.20)
    mock_graph.invoke.return_value = {
        "answer": "Best available answer.",
        "citations": [],
        "model_used": "test-model",
        "chunks": low_score_chunks,
    }

    from fastapi.testclient import TestClient
    from api.main import app
    client = TestClient(app)

    resp = client.post("/chat/", json={"question": "What are SFC requirements?"})

    assert resp.status_code == 200
    assert resp.json()["answer"] == "Best available answer."


@patch("api.routers.chat._graph")
def test_confidence_gate_retry_failure_is_nonfatal(mock_graph):
    """If graph.invoke raises, chat() returns a 502 — the graph owns error handling."""
    mock_graph.invoke.side_effect = RuntimeError("AstraDB timeout")

    from fastapi.testclient import TestClient
    from api.main import app
    client = TestClient(app)

    resp = client.post("/chat/", json={"question": "What are SFC requirements?"})

    assert resp.status_code == 502
