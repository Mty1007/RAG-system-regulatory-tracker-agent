"""Unit tests for core/online_eval.py.

Tests cover the pure-logic functions that do not require the ibm-watsonx-gov
SDK or any network calls:
  - _classify_gap() — all gap types and pass path
  - _avg_rerank() — basic average
  - EvalResult — attributes set correctly
  - evaluate() — graceful degradation when SDK is unavailable
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.online_eval import (
    EvalResult,
    _avg_rerank,
    _classify_gap,
    evaluate,
)


# ── _avg_rerank ───────────────────────────────────────────────────────────────

def test_avg_rerank_empty():
    assert _avg_rerank([]) == 0.0


def test_avg_rerank_single():
    assert _avg_rerank([{"rerank_score": 0.8}]) == 0.8


def test_avg_rerank_multiple():
    chunks = [{"rerank_score": 0.6}, {"rerank_score": 0.4}]
    assert _avg_rerank(chunks) == 0.5


def test_avg_rerank_missing_key():
    """Chunks without rerank_score default to 0.0."""
    chunks = [{"text": "hello"}, {"rerank_score": 0.5}]
    assert _avg_rerank(chunks) == 0.25


# ── _classify_gap ─────────────────────────────────────────────────────────────

def _chunks_with_rerank(score: float, n: int = 3) -> list[dict]:
    return [{"rerank_score": score, "text": f"chunk {i}"} for i in range(n)]


def test_classify_gap_pass():
    """Both metrics above threshold → None (no gap)."""
    scores = {"context_relevance": 0.7, "faithfulness": 0.8}
    assert _classify_gap(scores, _chunks_with_rerank(0.6), threshold=0.55) is None


def test_classify_gap_source_gap():
    """Low CR + low rerank → SOURCE_GAP."""
    scores = {"context_relevance": 0.3, "faithfulness": 0.7}
    # rerank score 0.2 < _RERANK_FLOOR (0.35)
    result = _classify_gap(scores, _chunks_with_rerank(0.2), threshold=0.55)
    assert result == "SOURCE_GAP"


def test_classify_gap_rephrase_gap():
    """Low faithfulness + acceptable CR → ANSWER_REPHRASE_GAP."""
    scores = {"context_relevance": 0.7, "faithfulness": 0.3}
    result = _classify_gap(scores, _chunks_with_rerank(0.6), threshold=0.55)
    assert result == "ANSWER_REPHRASE_GAP"


def test_classify_gap_both():
    """Both CR and faithfulness below threshold → BOTH."""
    scores = {"context_relevance": 0.3, "faithfulness": 0.3}
    result = _classify_gap(scores, _chunks_with_rerank(0.2), threshold=0.55)
    assert result == "BOTH"


def test_classify_gap_low_cr_high_rerank_treated_as_rephrase():
    """Low CR but high rerank (retrieval looks OK) → ANSWER_REPHRASE_GAP, not SOURCE_GAP."""
    scores = {"context_relevance": 0.4, "faithfulness": 0.7}
    # rerank score 0.8 > _RERANK_FLOOR (0.35) → not SOURCE_GAP
    result = _classify_gap(scores, _chunks_with_rerank(0.8), threshold=0.55)
    assert result == "ANSWER_REPHRASE_GAP"


def test_classify_gap_cjk_discount_prevents_false_rephrase():
    """Chinese question with FF=0.47 (below 0.55 but above 0.55*0.85=0.4675) must pass.

    Confirmed by live measurement: correct short Chinese answers score ~0.465-0.471 FF
    due to cross-lingual sentence-BERT underscoring — not a real faithfulness failure.
    """
    scores = {"context_relevance": 0.7, "faithfulness": 0.47}
    # Without CJK discount: 0.47 < 0.55 → ANSWER_REPHRASE_GAP
    assert _classify_gap(scores, _chunks_with_rerank(0.6), threshold=0.55) == "ANSWER_REPHRASE_GAP"
    # With CJK question: effective ff_threshold = 0.55 * 0.85 = 0.4675 → 0.47 passes
    result = _classify_gap(
        scores, _chunks_with_rerank(0.6), threshold=0.55,
        question="根據證監會規則，客戶虛擬資產中需要存放在冷存儲中的比例是多少？",
    )
    assert result is None, f"Expected None (pass) with CJK discount, got {result}"


# ── EvalResult ────────────────────────────────────────────────────────────────

def test_eval_result_passed_when_no_gap():
    r = EvalResult(
        scores={"context_relevance": 0.8, "faithfulness": 0.9},
        gap_type=None,
        threshold=0.55,
    )
    assert r.passed is True
    assert r.gap_type is None


def test_eval_result_failed_when_gap():
    r = EvalResult(
        scores={"context_relevance": 0.3, "faithfulness": 0.9},
        gap_type="SOURCE_GAP",
        threshold=0.55,
    )
    assert r.passed is False
    assert r.gap_type == "SOURCE_GAP"


# ── evaluate() — SDK graceful degradation ────────────────────────────────────

def test_evaluate_degrades_gracefully_when_sdk_unavailable():
    """If ibm-watsonx-gov is not installed, evaluate() returns zero scores
    (no gap → passes) without raising."""
    chunks = [{"text": "Some regulatory text.", "rerank_score": 0.4}]

    with patch("core.online_eval._score", return_value={"context_relevance": 0.0, "faithfulness": 0.0}):
        result = evaluate("What is the rule?", chunks, "The rule is X.")

    # Zero scores < default threshold (0.55) — gap should be classified.
    # The important thing is no exception is raised.
    assert isinstance(result, EvalResult)
    assert result.scores["context_relevance"] == 0.0
    assert result.scores["faithfulness"] == 0.0


def test_evaluate_passes_good_scores():
    """Scores above threshold → passed=True, gap_type=None."""
    chunks = [{"text": "A rule about client assets.", "rerank_score": 0.7}]

    with patch("core.online_eval._score", return_value={"context_relevance": 0.8, "faithfulness": 0.85}):
        result = evaluate("What rule applies to client assets?", chunks, "The rule requires segregation.")

    assert result.passed is True
    assert result.gap_type is None


def test_evaluate_threshold_override(monkeypatch):
    """ONLINE_EVAL_THRESHOLD env var changes the pass/fail boundary."""
    monkeypatch.setenv("ONLINE_EVAL_THRESHOLD", "0.90")
    chunks = [{"text": "Some text.", "rerank_score": 0.6}]

    # Scores of 0.7/0.8 pass at 0.55 but fail at 0.90
    with patch("core.online_eval._score", return_value={"context_relevance": 0.7, "faithfulness": 0.8}):
        result = evaluate("A question?", chunks, "An answer.")

    assert result.passed is False
    assert result.threshold == 0.90
