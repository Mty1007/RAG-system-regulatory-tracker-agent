"""Unit tests for core/agents nodes and graph (3-agent redesign).

Covers:
- planning_node decides source and strategy correctly
- retrieval_node returns chunks, avg_score, answer, citations, eval_result
- retrieval_node retries retrieval when confidence score is low
- retrieval_node returns PASS when answer overlaps with chunks
- retrieval_node returns RETRY on low faithfulness (no COS yet)
- retrieval_node returns LOW_CONFIDENCE on low faithfulness after COS fallback
- retrieval_node returns LOW_CONFIDENCE for empty answer
- cos_node re-generates and returns eval_result
- graph routes RETRY → cos_node
- graph routes PASS → END
- graph routes LOW_CONFIDENCE → END
- build_graph compiles without error
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


# ── helpers ───────────────────────────────────────────────────────────────────

def _chunk(id: str, score: float, text: str = "regulatory requirement text") -> dict:
    return {
        "_id": id,
        "doc_id": f"doc-{id}",
        "source": "SFC",
        "chunk_index": 0,
        "section_heading": "Test",
        "page_start": 1,
        "text": text,
        "token_count": 10,
        "rerank_score": score,
    }


def _gen_result(answer: str = "The SFC requires licensees to maintain records.") -> dict:
    return {
        "answer": answer,
        "citations": [{"doc_id": "sfc-abc", "source": "SFC",
                       "section_heading": "Records", "page_start": 5}],
        "model_used": "mistralai/mistral-medium-2505",
        "chunk_count": 3,
    }


# ── planning_node ─────────────────────────────────────────────────────────────

def test_planning_node_sfc_question():
    from core.agents.nodes import planning_node
    result = planning_node({"question": "What are SFC licensing requirements?"})
    assert result["plan"]["source"] == "SFC"
    assert result["plan"]["strategy"] == "hybrid"


def test_planning_node_pcpd_question():
    from core.agents.nodes import planning_node
    result = planning_node({"question": "What are the PCPD data protection principles?"})
    assert result["plan"]["source"] == "PCPD"


def test_planning_node_both_sources():
    from core.agents.nodes import planning_node
    result = planning_node({"question": "What are the regulatory requirements?"})
    assert result["plan"]["source"] is None  # search both


def test_planning_node_keyword_strategy():
    from core.agents.nodes import planning_node
    result = planning_node({"question": "What does section 5 clause 3 say about SFC?"})
    assert result["plan"]["strategy"] == "keyword"


def test_planning_node_source_override():
    from core.agents.nodes import planning_node
    result = planning_node({
        "question": "What are the data protection rules?",
        "source_filter": "SFC",
    })
    assert result["plan"]["source"] == "SFC"  # override wins even for PCPD-sounding question


# ── retrieval_node ────────────────────────────────────────────────────────────

@patch("core.agents.nodes.log_request")
@patch("core.agents.nodes.generate_answer")
@patch("core.agents.nodes.rerank")
@patch("api.routers.chat._retrieve_candidates")
def test_retrieval_node_returns_pass_on_good_answer(mock_retrieve, mock_rerank, mock_generate, mock_log):
    from core.agents.nodes import retrieval_node

    chunks = [_chunk("a", 0.8, text="the SFC requires licensees to maintain records for seven years")]
    mock_retrieve.return_value = chunks
    mock_rerank.return_value = chunks
    mock_generate.return_value = _gen_result(
        "The SFC requires licensees to maintain records."
    )

    result = retrieval_node({
        "question":      "What are SFC requirements?",
        "source_filter": None,
        "retrieve_n":    40,
        "top_k":         10,
        "plan":          {},
        "cos_fallback":  False,
        "retry_count":   0,
    })

    assert "chunks" in result
    assert "avg_score" in result
    assert "answer" in result
    assert "eval_result" in result
    assert result["eval_result"] == "PASS"
    assert result["avg_score"] == pytest.approx(0.8)


@patch("core.agents.nodes.log_request")
@patch("core.agents.nodes.generate_answer")
@patch("core.agents.nodes.rerank")
@patch("api.routers.chat._retrieve_candidates")
def test_retrieval_node_returns_retry_on_low_faithfulness(mock_retrieve, mock_rerank, mock_generate, mock_log):
    from core.agents.nodes import retrieval_node

    chunks = [_chunk("a", 0.8, text="completely different regulatory content about PCPD")]
    mock_retrieve.return_value = chunks
    mock_rerank.return_value = chunks
    # Answer has no overlap with chunk text
    mock_generate.return_value = _gen_result("xyz abc foo bar baz qux quux quuz")

    result = retrieval_node({
        "question":      "question?",
        "source_filter": None,
        "retrieve_n":    40,
        "top_k":         10,
        "plan":          {},
        "cos_fallback":  False,
        "retry_count":   0,
    })

    assert result["eval_result"] == "RETRY"


@patch("core.agents.nodes.log_request")
@patch("core.agents.nodes.generate_answer")
@patch("core.agents.nodes.rerank")
@patch("api.routers.chat._retrieve_candidates")
def test_retrieval_node_returns_low_confidence_after_cos(mock_retrieve, mock_rerank, mock_generate, mock_log):
    from core.agents.nodes import retrieval_node

    chunks = [_chunk("a", 0.8, text="completely different regulatory content about PCPD")]
    mock_retrieve.return_value = chunks
    mock_rerank.return_value = chunks
    mock_generate.return_value = _gen_result("xyz abc foo bar baz qux quux quuz")

    result = retrieval_node({
        "question":      "question?",
        "source_filter": None,
        "retrieve_n":    40,
        "top_k":         10,
        "plan":          {},
        "cos_fallback":  True,   # COS already ran → no RETRY allowed
        "retry_count":   0,
    })

    assert result["eval_result"] == "LOW_CONFIDENCE"


@patch("core.agents.nodes.log_request")
@patch("core.agents.nodes.generate_answer")
@patch("core.agents.nodes.rerank")
@patch("api.routers.chat._retrieve_candidates")
def test_retrieval_node_low_confidence_on_empty_answer(mock_retrieve, mock_rerank, mock_generate, mock_log):
    from core.agents.nodes import retrieval_node

    chunks = [_chunk("a", 0.8)]
    mock_retrieve.return_value = chunks
    mock_rerank.return_value = chunks
    mock_generate.return_value = _gen_result("")  # empty answer

    result = retrieval_node({
        "question":      "question?",
        "source_filter": None,
        "retrieve_n":    40,
        "top_k":         10,
        "plan":          {},
        "cos_fallback":  False,
        "retry_count":   0,
    })

    assert result["eval_result"] == "LOW_CONFIDENCE"


@patch("core.agents.nodes.log_request")
@patch("core.agents.nodes.generate_answer")
@patch("core.agents.nodes.rerank")
@patch("api.routers.chat._retrieve_candidates")
def test_retrieval_node_retries_on_low_confidence_score(mock_retrieve, mock_rerank, mock_generate, mock_log):
    """When initial AstraDB score < 0.30, retry with wider pool is attempted."""
    from core.agents.nodes import retrieval_node

    low_chunks  = [_chunk("a", 0.10, text="regulatory requirement text")]
    high_chunks = [_chunk("b", 0.80, text="regulatory requirement text")]
    mock_retrieve.side_effect = [low_chunks, high_chunks]
    mock_rerank.return_value = high_chunks
    mock_generate.return_value = _gen_result(
        "regulatory requirement text answer"
    )

    retrieval_node({
        "question":      "What are SFC requirements?",
        "source_filter": None,
        "retrieve_n":    40,
        "top_k":         10,
        "plan":          {},
        "cos_fallback":  False,
        "retry_count":   0,
    })

    # _retrieve_candidates called twice: initial + confidence-gate retry
    assert mock_retrieve.call_count == 2


# ── cos_node ──────────────────────────────────────────────────────────────────

@patch("core.agents.nodes.log_request")
@patch("core.agents.nodes.generate_answer")
def test_cos_node_returns_low_confidence_when_no_doc_ids(mock_generate, mock_log):
    from core.agents.nodes import cos_node

    # Chunks have no doc_id → COS fallback skips and returns LOW_CONFIDENCE
    result = cos_node({
        "question": "What are SFC requirements?",
        "chunks":   [{"_id": "x", "text": "text", "rerank_score": 0.5}],
        "top_k":    10,
        "cos_fallback": False,
    })

    assert result["cos_fallback"] is True
    assert result["eval_result"] == "LOW_CONFIDENCE"
    mock_generate.assert_not_called()


# ── graph routing ─────────────────────────────────────────────────────────────

def test_graph_route_retry_returns_cos():
    """RETRY → cos node."""
    from core.agents.graph import _route_after_retrieval

    assert _route_after_retrieval({"eval_result": "RETRY"}) == "cos"


def test_graph_route_pass_returns_end():
    from langgraph.graph import END
    from core.agents.graph import _route_after_retrieval

    assert _route_after_retrieval({"eval_result": "PASS"}) == END


def test_graph_route_low_confidence_returns_end():
    from langgraph.graph import END
    from core.agents.graph import _route_after_retrieval

    assert _route_after_retrieval({"eval_result": "LOW_CONFIDENCE"}) == END


def test_graph_route_default_returns_end():
    """Missing eval_result defaults to PASS → END."""
    from langgraph.graph import END
    from core.agents.graph import _route_after_retrieval

    assert _route_after_retrieval({}) == END


def test_build_graph_compiles():
    """build_graph() should return a compiled graph without error."""
    from core.agents.graph import build_graph
    graph = build_graph()
    assert graph is not None
