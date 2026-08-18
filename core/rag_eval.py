"""RAG response quality logging and monitoring.

Two responsibilities
--------------------
1. **Eval-log writer** — appends one CSV row per request to ``rag_eval_log.csv``
   using the exact column names the ibm-watsonx-gov SDK expects by default:
       record_id, input_text, context1, context2, context3,
       generated_text, ground_truth

   This file can be fed directly to MetricsEvaluator.evaluate() for offline
   scoring of context_relevance, faithfulness, and answer_relevance.

2. **Rerank quality warning** — computes avg / min rerank_score from the
   chunks the LLM saw and emits a WARNING when the retrieval looks weak.
   This is a zero-extra-API-call proxy for context relevance.

Usage
-----
Called from api/routers/chat.py after generate_answer() returns:

    from core.rag_eval import log_request

    log_request(
        question=req.question,
        chunks=top_chunks,
        answer=result["answer"],
    )
"""

from __future__ import annotations

import csv
import logging
import os
import pathlib
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Path to the rolling eval log — override via RAG_EVAL_LOG env var.
_DEFAULT_LOG_PATH = "rag_eval_log.csv"

# Rerank score below which we emit a WARNING.
# Tune this once you have a few hundred rows of baseline data.
_RERANK_WARN_THRESHOLD = 0.30

# CSV column order — must match ibm-watsonx-gov SDK default field roles exactly.
_CSV_COLUMNS = [
    "record_id",
    "input_text",
    "context1",
    "context2",
    "context3",
    "generated_text",
    "ground_truth",
]


def _log_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("RAG_EVAL_LOG", _DEFAULT_LOG_PATH))


def _ensure_header(path: pathlib.Path) -> None:
    """Write the CSV header row if the file does not yet exist."""
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
) -> None:
    """Append one row to the eval log and check rerank quality.

    Parameters
    ----------
    question:
        The user's original question (maps to ``input_text``).
    chunks:
        The reranked chunks passed to the LLM (up to 3 are logged as context).
    answer:
        The LLM-generated answer (maps to ``generated_text``).
    ground_truth:
        Optional reference answer for offline answer_similarity scoring.
        Leave empty for live traffic rows.
    """
    _check_rerank_quality(chunks, question)

    path = _log_path()
    _ensure_header(path)

    row = {
        "record_id":      str(uuid.uuid4()),
        "input_text":     question,
        "context1":       chunks[0]["text"] if len(chunks) > 0 else "",
        "context2":       chunks[1]["text"] if len(chunks) > 1 else "",
        "context3":       chunks[2]["text"] if len(chunks) > 2 else "",
        "generated_text": answer,
        "ground_truth":   ground_truth,
    }

    with path.open("a", newline="", encoding="utf-8") as fh:
        csv.DictWriter(fh, fieldnames=_CSV_COLUMNS).writerow(row)

    logger.debug("rag_eval: logged record_id=%s", row["record_id"])
