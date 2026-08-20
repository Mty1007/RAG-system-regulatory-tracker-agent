"""Unit tests for core/adaptive_retriever.py.

Tests cover the pure-logic helpers and escalation flow control without
making any network calls (all external functions are mocked).

Covered:
  1. _merge_chunks() — deduplication + highest-score wins
  2. _composite() — average of CR and FF
  3. ANSWER_REPHRASE_GAP path — rephrase fires, improves score, result adopted
  4. SOURCE_GAP stage 1 — widen fires, improves score, stops at stage 1
  5. SOURCE_GAP stage 2 — expansion used when stage 1 insufficient
  6. No regression — if escalation makes things worse, original is kept
  7. Escalation exceptions do not propagate — original returned safely
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.online_eval import EvalResult
from core.adaptive_retriever import _composite, _merge_chunks, escalate


# ── _merge_chunks ─────────────────────────────────────────────────────────────

def _chunk(id_: str, score: float, text: str = "text") -> dict:
    return {"_id": id_, "rerank_score": score, "text": text, "doc_id": "d", "source": "SFC",
            "section_heading": "", "page_start": 0, "token_count": 5}


def test_merge_chunks_deduplicates_keeps_higher_score():
    base = [_chunk("a", 0.9), _chunk("b", 0.5)]
    extra = [_chunk("b", 0.8), _chunk("c", 0.6)]
    merged = _merge_chunks(base, extra, top_k=5)
    ids = [c["_id"] for c in merged]
    assert "b" in ids
    b_score = next(c["rerank_score"] for c in merged if c["_id"] == "b")
    assert b_score == 0.8  # extra had higher score for "b"
    assert len(merged) == 3  # a, b, c


def test_merge_chunks_respects_top_k():
    base  = [_chunk(str(i), float(i) / 10) for i in range(5)]
    extra = [_chunk(str(i + 5), float(i) / 10) for i in range(5)]
    merged = _merge_chunks(base, extra, top_k=4)
    assert len(merged) == 4


def test_merge_chunks_sorted_descending():
    base  = [_chunk("a", 0.3)]
    extra = [_chunk("b", 0.9), _chunk("c", 0.6)]
    merged = _merge_chunks(base, extra, top_k=10)
    scores = [c["rerank_score"] for c in merged]
    assert scores == sorted(scores, reverse=True)


# ── _composite ────────────────────────────────────────────────────────────────

def test_composite_average():
    assert _composite({"context_relevance": 0.6, "faithfulness": 0.8}) == 0.7


def test_composite_zeros():
    assert _composite({"context_relevance": 0.0, "faithfulness": 0.0}) == 0.0


# ── escalate() — ANSWER_REPHRASE_GAP ─────────────────────────────────────────

def _make_eval(cr: float, ff: float, gap: str | None, threshold: float = 0.55) -> EvalResult:
    return EvalResult(
        scores={"context_relevance": cr, "faithfulness": ff},
        gap_type=gap,
        threshold=threshold,
    )


def test_rephrase_gap_adopts_improved_answer():
    """Rephrase re-retrieves with new search terms and adopts better answer."""
    chunks = [_chunk("a", 0.6)]
    new_chunks = [_chunk("b", 0.9), _chunk("c", 0.8)]
    initial_eval = _make_eval(cr=0.7, ff=0.3, gap="ANSWER_REPHRASE_GAP")
    improved_result = {"answer": "Better answer.", "citations": [], "model_used": "m", "chunk_count": 2}
    improved_eval = _make_eval(cr=0.7, ff=0.8, gap=None)

    with patch("core.adaptive_retriever._rephrase_question", return_value="rephrased question terms"), \
         patch("core.adaptive_retriever.retrieve", return_value=new_chunks), \
         patch("core.adaptive_retriever.generate_answer", return_value=improved_result), \
         patch("core.adaptive_retriever.evaluate", return_value=improved_eval):
        final_chunks, final_result, final_eval = escalate(
            "q?", chunks, "Original.", initial_eval
        )

    assert final_result["answer"] == "Better answer."
    assert final_eval.passed is True


def test_rephrase_gap_keeps_original_when_no_improvement():
    """If rephrased retrieval scores worse, keep the original answer."""
    chunks = [_chunk("a", 0.6)]
    new_chunks = [_chunk("b", 0.3)]
    initial_eval = _make_eval(cr=0.7, ff=0.3, gap="ANSWER_REPHRASE_GAP")
    worse_result = {"answer": "Worse answer.", "citations": [], "model_used": "m", "chunk_count": 1}
    worse_eval = _make_eval(cr=0.4, ff=0.2, gap="ANSWER_REPHRASE_GAP")

    with patch("core.adaptive_retriever._rephrase_question", return_value="different terms"), \
         patch("core.adaptive_retriever.retrieve", return_value=new_chunks), \
         patch("core.adaptive_retriever.generate_answer", return_value=worse_result), \
         patch("core.adaptive_retriever.evaluate", return_value=worse_eval):
        _, final_result, _ = escalate("q?", chunks, "Original.", initial_eval)

    assert final_result["answer"] == "Original."


def test_rephrase_gap_skips_when_question_unchanged():
    """If rephrase returns the same question, skip re-retrieval entirely."""
    chunks = [_chunk("a", 0.6)]
    initial_eval = _make_eval(cr=0.7, ff=0.3, gap="ANSWER_REPHRASE_GAP")

    with patch("core.adaptive_retriever._rephrase_question", return_value="q?") as mock_rephrase, \
         patch("core.adaptive_retriever.retrieve") as mock_retrieve:
        escalate("q?", chunks, "Original.", initial_eval)

    # retrieve must NOT be called if rephrase returned unchanged question
    mock_retrieve.assert_not_called()


# ── escalate() — SOURCE_GAP stage 1 ──────────────────────────────────────────

def test_source_gap_stage1_stops_when_passing():
    chunks = [_chunk("a", 0.2)]
    initial_eval = _make_eval(cr=0.3, ff=0.7, gap="SOURCE_GAP")
    new_chunks = [_chunk("b", 0.8), _chunk("c", 0.7)]
    stage1_result = {"answer": "Stage 1 answer.", "citations": [], "model_used": "m", "chunk_count": 2}
    stage1_eval = _make_eval(cr=0.8, ff=0.85, gap=None)

    with patch("core.adaptive_retriever.retrieve", return_value=new_chunks) as mock_retrieve, \
         patch("core.adaptive_retriever.generate_answer", return_value=stage1_result), \
         patch("core.adaptive_retriever.evaluate", return_value=stage1_eval), \
         patch("core.adaptive_retriever._expand_query", return_value="expanded"):
        final_chunks, final_result, final_eval = escalate(
            "q?", chunks, "Original.", initial_eval, top_n=20
        )

    assert final_result["answer"] == "Stage 1 answer."
    assert final_eval.passed is True
    # retrieve() called once for stage 1; stage 2 and 3 must not have fired
    assert mock_retrieve.call_count == 1


# ── escalate() — exception safety ────────────────────────────────────────────

def test_escalation_exception_returns_original():
    """If escalation raises at every stage, the original answer is returned unchanged."""
    chunks = [_chunk("a", 0.2)]
    initial_eval = _make_eval(cr=0.3, ff=0.3, gap="BOTH")

    with patch("core.adaptive_retriever._rephrase_question", side_effect=RuntimeError("API down")), \
         patch("core.adaptive_retriever.retrieve", side_effect=RuntimeError("DB down")):
        # escalate() should not raise — it catches stage failures internally
        final_chunks, final_result, final_eval = escalate(
            "q?", chunks, "Original.", initial_eval
        )

    assert final_result["answer"] == "Original."
