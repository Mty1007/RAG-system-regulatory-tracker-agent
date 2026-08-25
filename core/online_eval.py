"""Online (per-request) RAG quality evaluator.

Runs synchronously on every /chat request — the HTTP response is held until
evaluation and any escalation completes, so the user always receives the best
possible answer in a single round-trip.

Scoring
-------
Uses ibm-watsonx-gov MetricsEvaluator (same SDK as the offline evaluator in
scripts/eval_quality.py).  Two metrics are scored:

* **context_relevance**  — were the right chunks retrieved?
* **faithfulness**       — is the answer grounded in the retrieved context?

No ground truth is required for either metric, making them suitable for
live scoring without a reference answer set.

Gap analysis
------------
When any score is below ONLINE_EVAL_THRESHOLD the failure is classified:

    SOURCE_GAP           context_relevance < threshold (avg rerank_score also low)
    ANSWER_REPHRASE_GAP  faithfulness < threshold but context_relevance >= threshold
    BOTH                 both metrics fail

Remediation is delegated to core/adaptive_retriever.py which is called from
api/routers/chat.py after evaluate() returns a non-None gap type.

Required environment variables
-------------------------------
WATSONX_API_KEY      (bridged to WATSONX_APIKEY for the gov SDK)
WATSONX_PROJECT_ID
WATSONX_URL
ONLINE_EVAL_THRESHOLD   float 0-1, default 0.55
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Literal, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

GapType = Literal["SOURCE_GAP", "ANSWER_REPHRASE_GAP", "BOTH"]

_DEFAULT_THRESHOLD = 0.55

# Rerank score floor below which we additionally classify the retrieval as weak.
# Used together with context_relevance to confirm a SOURCE_GAP.
_RERANK_FLOOR = 0.35

# Cross-lingual faithfulness discount factor.
# The sentence-BERT faithfulness model (sentence_bert_mini_lm) scores
# cross-lingual pairs (CJK question vs English chunks) systematically lower
# than same-language pairs — confirmed at ~0.45–0.49 for correct short Chinese
# answers grounded in English regulatory text.  When the question is Chinese
# and chunks are English, we reduce the effective threshold for faithfulness
# by this factor to avoid false-positive ANSWER_REPHRASE_GAP escalations on
# already-correct short answers.
_CJK_FF_DISCOUNT = 0.85   # effective threshold = threshold * 0.85
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _is_chinese(text: str) -> bool:
    return len(_CJK_RE.findall(text)) >= 3


def _threshold() -> float:
    try:
        return float(os.environ.get("ONLINE_EVAL_THRESHOLD", _DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD


# ── SDK env bridge ─────────────────────────────────────────────────────────────
# ibm-watsonx-gov reads WATSONX_APIKEY (no underscore before KEY).
# The project uses WATSONX_API_KEY everywhere else.  Bridge lazily so this
# module has no import-time side-effects.

def _ensure_gov_env() -> None:
    if "WATSONX_APIKEY" not in os.environ and "WATSONX_API_KEY" in os.environ:
        os.environ["WATSONX_APIKEY"] = os.environ["WATSONX_API_KEY"]


# ── scoring ───────────────────────────────────────────────────────────────────

def _score(
    question: str,
    chunks: list[dict[str, Any]],
    answer: str,
) -> dict[str, float]:
    """Return {'context_relevance': float, 'faithfulness': float}.

    Calls ibm-watsonx-gov MetricsEvaluator with a single-row DataFrame.
    Returns zero scores on any SDK error so the caller degrades gracefully.
    """
    _ensure_gov_env()

    try:
        from ibm_watsonx_gov.config import GenAIConfiguration
        from ibm_watsonx_gov.evaluators import MetricsEvaluator
        from ibm_watsonx_gov.metrics.context_relevance.context_relevance_metric import (
            ContextRelevanceMetric,
        )
        from ibm_watsonx_gov.metrics.faithfulness.faithfulness_metric import (
            FaithfulnessMetric,
        )
    except ImportError as exc:
        logger.warning("online_eval: ibm-watsonx-gov not available: %s", exc)
        return {"context_relevance": 0.0, "faithfulness": 0.0}

    # Build a single-row DataFrame with context columns matching the SDK schema.
    context_cols = [f"context{i+1}" for i in range(len(chunks))]
    row: dict[str, Any] = {"input_text": question, "generated_text": answer}
    for i, col in enumerate(context_cols):
        row[col] = chunks[i]["text"]

    df = pd.DataFrame([row])

    cfg = GenAIConfiguration(
        input_fields=["input_text"],
        context_fields=context_cols,
        output_fields=["generated_text"],
    )

    try:
        evaluator = MetricsEvaluator(configuration=cfg)
        results = evaluator.evaluate(
            df,
            metrics=[
                ContextRelevanceMetric(method="sentence_bert_bge"),
                FaithfulnessMetric(method="sentence_bert_mini_lm"),
            ],
        )
    except Exception as exc:
        logger.warning("online_eval: scoring failed: %s", exc)
        return {"context_relevance": 0.0, "faithfulness": 0.0}

    scores: dict[str, float] = {}
    for m in results.metrics_result:
        name = m.display_name.lower().replace(" ", "_")
        scores[name] = float(m.value or 0.0)

    # Normalise key names — SDK display names vary slightly across versions.
    cr = scores.get("context_relevance", scores.get("context_relevance_score", 0.0))
    ff = scores.get("faithfulness", scores.get("faithfulness_score", 0.0))
    return {"context_relevance": cr, "faithfulness": ff}


# ── gap analysis ──────────────────────────────────────────────────────────────

def _avg_rerank(chunks: list[dict[str, Any]]) -> float:
    if not chunks:
        return 0.0
    scores = [c.get("rerank_score", 0.0) for c in chunks]
    return sum(scores) / len(scores)


def _classify_gap(
    scores: dict[str, float],
    chunks: list[dict[str, Any]],
    threshold: float,
    question: str = "",
) -> Optional[GapType]:
    """Return the gap type or None if quality is acceptable.

    For Chinese questions evaluated against English chunks, the faithfulness
    threshold is discounted by _CJK_FF_DISCOUNT to account for the systematic
    underscoring of cross-lingual pairs by sentence-BERT faithfulness models.
    """
    cr_fail = scores["context_relevance"] < threshold

    # Apply cross-lingual discount to faithfulness threshold when the question
    # is Chinese — prevents false ANSWER_REPHRASE_GAP on already-correct short
    # answers that just score low due to language mismatch in the scorer.
    ff_threshold = threshold
    if question and _is_chinese(question):
        ff_threshold = threshold * _CJK_FF_DISCOUNT
        logger.debug(
            "online_eval: CJK question — applying ff_threshold discount %.2f → %.2f",
            threshold,
            ff_threshold,
        )
    ff_fail = scores["faithfulness"] < ff_threshold

    if not cr_fail and not ff_fail:
        return None

    if cr_fail and ff_fail:
        return "BOTH"

    # SOURCE_GAP: poor retrieval confirmed by both the CR metric AND low rerank
    # scores.  Low rerank alone (without low CR) means the chunks are acceptable
    # but the evaluator just hasn't given them a high score yet — not enough
    # signal to trigger expensive re-retrieval.
    if cr_fail and _avg_rerank(chunks) < _RERANK_FLOOR:
        return "SOURCE_GAP"

    if ff_fail:
        return "ANSWER_REPHRASE_GAP"

    # CR failing but rerank is acceptable — treat as rephrase gap since the
    # retrieved content looks right but the answer didn't reflect it faithfully.
    return "ANSWER_REPHRASE_GAP"


# ── public API ────────────────────────────────────────────────────────────────

class EvalResult:
    """Outcome of evaluate().  Passed back to chat.py for block-and-replace."""

    def __init__(
        self,
        scores: dict[str, float],
        gap_type: Optional[GapType],
        threshold: float,
    ) -> None:
        self.scores = scores
        self.gap_type = gap_type
        self.threshold = threshold
        self.passed = gap_type is None

    def __repr__(self) -> str:
        return (
            f"EvalResult(cr={self.scores['context_relevance']:.3f} "
            f"ff={self.scores['faithfulness']:.3f} "
            f"gap={self.gap_type} passed={self.passed})"
        )


def evaluate(
    question: str,
    chunks: list[dict[str, Any]],
    answer: str,
) -> EvalResult:
    """Score the response and classify any quality gap.

    Parameters
    ----------
    question:
        The user's original question.
    chunks:
        All chunks passed to generate_answer() (up to top_k).
    answer:
        The generated answer string.

    Returns
    -------
    EvalResult with .passed, .gap_type, and .scores.
    """
    threshold = _threshold()
    scores = _score(question, chunks, answer)
    gap_type = _classify_gap(scores, chunks, threshold, question=question)

    logger.info(
        "online_eval: cr=%.3f ff=%.3f gap=%s threshold=%.2f",
        scores["context_relevance"],
        scores["faithfulness"],
        gap_type,
        threshold,
    )
    return EvalResult(scores=scores, gap_type=gap_type, threshold=threshold)
