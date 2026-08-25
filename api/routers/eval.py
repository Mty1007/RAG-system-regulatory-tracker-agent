"""Eval router — POST /eval/online

Exposes the online evaluator as an explicit HTTP endpoint so IBM watsonx
Orchestrate can call it as a standalone tool (e.g. in a flow that does
ask → check quality → escalate → return improved answer).

The endpoint is intentionally thin: it accepts the same fields that
/chat/ produces, delegates all logic to core/online_eval and
core/adaptive_retriever, and returns a structured quality report.

This router is mounted at /eval by api/main.py.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.adaptive_retriever import escalate
from core.online_eval import evaluate

router = APIRouter()


# ── request / response schemas ────────────────────────────────────────────────

class OnlineEvalRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    chunks: list[dict[str, Any]] = Field(
        ...,
        description="Reranked chunk dicts as returned by /chat/ internals. "
                    "Each must have at least: text, rerank_score, doc_id, source.",
    )
    answer: str = Field(..., min_length=1)
    source_filter: Optional[str] = Field(
        default=None,
        description="SFC or PCPD — passed to adaptive retriever if escalation fires.",
    )
    top_k: int = Field(default=15, ge=1, le=20)


class OnlineEvalResponse(BaseModel):
    passed: bool
    gap_type: Optional[str]
    context_relevance: float
    faithfulness: float
    threshold: float
    escalated: bool
    improved_answer: Optional[str] = None
    final_context_relevance: Optional[float] = None
    final_faithfulness: Optional[float] = None


# ── endpoint ──────────────────────────────────────────────────────────────────

@router.post("/online", response_model=OnlineEvalResponse)
def online_eval(req: OnlineEvalRequest) -> OnlineEvalResponse:
    """Score an answer and optionally escalate to improve it.

    Runs the same evaluation + adaptive retrieval loop as the /chat/ endpoint
    but accepts pre-computed chunks and an answer so Orchestrate can trigger
    it on demand (e.g. to re-evaluate a response that was cached or replayed).
    """
    source = req.source_filter.upper() if req.source_filter else None
    if source and source not in {"SFC", "PCPD"}:
        raise HTTPException(
            status_code=422,
            detail=f"source_filter must be one of SFC, PCPD (got '{source}')",
        )

    try:
        result = evaluate(req.question, req.chunks, req.answer)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Eval error: {exc}") from exc

    if result.passed:
        return OnlineEvalResponse(
            passed=True,
            gap_type=None,
            context_relevance=result.scores["context_relevance"],
            faithfulness=result.scores["faithfulness"],
            threshold=result.threshold,
            escalated=False,
        )

    # Escalate
    try:
        _, improved, final_eval = escalate(
            req.question,
            req.chunks,
            req.answer,
            result,
            source_filter=source,
            top_k=req.top_k,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Escalation error: {exc}") from exc

    return OnlineEvalResponse(
        passed=final_eval.passed,
        gap_type=result.gap_type,
        context_relevance=result.scores["context_relevance"],
        faithfulness=result.scores["faithfulness"],
        threshold=result.threshold,
        escalated=True,
        improved_answer=improved.get("answer"),
        final_context_relevance=final_eval.scores["context_relevance"],
        final_faithfulness=final_eval.scores["faithfulness"],
    )
