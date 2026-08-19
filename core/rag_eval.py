"""RAG response quality logging and monitoring.

Two responsibilities
--------------------
1. **Eval-log writer** — appends one CSV row per request to ``rag_eval_log.csv``
   using the exact column names the ibm-watsonx-gov SDK expects by default:
       record_id, input_text, context1 … contextN,
       generated_text, ground_truth

   The number of context columns matches the number of chunks actually passed
   to the LLM (up to MAX_CONTEXT_COLS=20).  This ensures the faithfulness and
   context-relevance scorers see the same context the LLM used.

   Escalation metadata columns (gap_type, pre_score, post_score) are appended
   when the online evaluator triggered adaptive retrieval.  These columns are
   empty for clean requests and allow offline analysis of where escalation
   was needed and whether it helped.

   This file can be fed directly to MetricsEvaluator.evaluate() for offline
   scoring of context_relevance, faithfulness, and answer_relevance.

2. **Rerank quality warning** — computes avg / min rerank_score from the
   chunks the LLM saw and emits a WARNING when the retrieval looks weak.
   This is a zero-extra-API-call proxy for context relevance.

Usage
-----
Called from api/routers/chat.py after generate_answer() and online eval return:

    from core.rag_eval import log_request

    log_request(
        question=req.question,
        chunks=top_chunks,
        answer=result["answer"],
        gap_type=eval_result.gap_type,       # None if quality was acceptable
        pre_score=0.42,                       # composite score before escalation
        post_score=0.67,                      # composite score after escalation
    )
"""

from __future__ import annotations

import csv
import logging
import os
import pathlib
import threading
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Path to the rolling eval log — override via RAG_EVAL_LOG env var.
_DEFAULT_LOG_PATH = "rag_eval_log.csv"

# Rerank score below which we emit a WARNING.
_RERANK_WARN_THRESHOLD = 0.30

# Maximum number of context columns to log.
# Raised from 10 → 20 to match the new top_k default (15) and give the
# offline evaluator full visibility into all chunks the LLM actually received.
MAX_CONTEXT_COLS = 20

# Base columns; context columns are added dynamically up to MAX_CONTEXT_COLS.
_BASE_COLUMNS = ["record_id", "input_text"]
_CONTEXT_COLUMNS = [f"context{i}" for i in range(1, MAX_CONTEXT_COLS + 1)]
_TAIL_COLUMNS = ["generated_text", "ground_truth", "gap_type", "pre_score", "post_score"]
_CSV_COLUMNS = _BASE_COLUMNS + _CONTEXT_COLUMNS + _TAIL_COLUMNS

# Lock protecting the CSV header check-then-write so concurrent requests
# in the same process cannot both see "file absent" and double-write headers.
_csv_lock = threading.Lock()


def _log_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("RAG_EVAL_LOG", _DEFAULT_LOG_PATH))


def _ensure_header(path: pathlib.Path) -> None:
    """Write the CSV header row if the file does not yet exist.

    Called under _csv_lock so the check-then-create is atomic within a process.
    """
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=_CSV_COLUMNS).writeheader()


def _check_rerank_quality(chunks: list[dict[str, Any]], question: str) -> None:
    """Warn when average rerank score is below threshold."""
    if not chunks:
        return
    scores = [c.get("rerank_score", 0.0) for c in chunks]
    avg_score = sum(scores) / len(scores)
    min_score = min(scores)
    if avg_score < _RERANK_WARN_THRESHOLD:
        logger.warning(
            "rag_eval: low context relevance — avg_rerank=%.3f  min_rerank=%.3f"
            "  chunks=%d  question=%r",
            avg_score,
            min_score,
            len(chunks),
            question[:80],
        )
    else:
        logger.debug(
            "rag_eval: avg_rerank=%.3f  min_rerank=%.3f  chunks=%d",
            avg_score,
            min_score,
            len(chunks),
        )


def log_request(
    question: str,
    chunks: list[dict[str, Any]],
    answer: str,
    ground_truth: str = "",
    gap_type: Optional[str] = None,
    pre_score: Optional[float] = None,
    post_score: Optional[float] = None,
) -> None:
    """Append one row to the eval log and check rerank quality.

    Parameters
    ----------
    question:
        The user's original question (maps to ``input_text``).
    chunks:
        The reranked chunks passed to the LLM — all are logged as context
        columns (context1 … contextN) so the offline scorer sees exactly what
        the LLM saw.
    answer:
        The LLM-generated answer (maps to ``generated_text``).
    ground_truth:
        Optional reference answer for offline answer_similarity scoring.
        Leave empty for live traffic rows.
    gap_type:
        Gap classification from online_eval: SOURCE_GAP, ANSWER_REPHRASE_GAP,
        BOTH, or None (no escalation needed).
    pre_score:
        Composite quality score before escalation (None if no escalation).
    post_score:
        Composite quality score after escalation (None if no escalation).
    """
    _check_rerank_quality(chunks, question)

    path = _log_path()

    row: dict[str, Any] = {
        "record_id":      str(uuid.uuid4()),
        "input_text":     question,
        "generated_text": answer,
        "ground_truth":   ground_truth,
        "gap_type":       gap_type or "",
        "pre_score":      "" if pre_score is None else f"{pre_score:.3f}",
        "post_score":     "" if post_score is None else f"{post_score:.3f}",
    }
    # Log every chunk the LLM actually received, up to MAX_CONTEXT_COLS.
    for i, col in enumerate(_CONTEXT_COLUMNS):
        row[col] = chunks[i]["text"] if i < len(chunks) else ""

    with _csv_lock:
        _ensure_header(path)
        with path.open("a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=_CSV_COLUMNS).writerow(row)

    logger.debug("rag_eval: logged record_id=%s  gap=%s", row["record_id"], gap_type or "none")
