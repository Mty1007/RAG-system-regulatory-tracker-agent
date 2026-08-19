"""Chat API router — POST /chat

Wires together:
  retrieve() → COS fallback → rerank() → generate_answer()
  → online_eval() → adaptive escalation → log_request()

The online evaluator runs synchronously (block-and-replace): if the first
answer scores below ONLINE_EVAL_THRESHOLD, the adaptive retriever tries to
improve it before the HTTP response is returned.  The user always receives
the best possible answer in a single round-trip.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.adaptive_retriever import escalate
from core.cos_retriever import fetch_fallback_passages
from core.generator import generate_answer
from core.online_eval import evaluate
from core.rag_eval import log_request
from core.reranker import rerank
from core.retriever import retrieve

router = APIRouter()


# ── request / response schemas ────────────────────────────────────────────────

class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    source_filter: Optional[str] = Field(
        default=None,
        description="Restrict retrieval to one regulator: SFC or PCPD. "
                    "Omit or pass null to search all sources.",
    )
    top_k: int = Field(
        default=15,
        ge=1,
        le=20,
        description="Number of chunks to pass to the LLM after reranking.",
    )


class CitationOut(BaseModel):
    doc_id: str
    source: str
    section_heading: str
    page_start: int


class ChatResponse(BaseModel):
    answer: str
    citations: list[CitationOut]
    model_used: str
    chunk_count: int
    eval_passed: bool = True
    gap_type: Optional[str] = None
    context_relevance: Optional[float] = None
    faithfulness: Optional[float] = None


# ── endpoint ──────────────────────────────────────────────────────────────────

@router.post("/", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """Answer a regulatory question using hybrid RAG with online quality gating.

    Pipeline:
    1. Hybrid ANN + BM25 search on AstraDB via find_and_rerank().
    2. COS full-doc fallback — appends wider Markdown passages when top
       chunk rerank_score is weak (requires USE_COS env var).
    3. NVIDIA reranker already applied inside retrieve(); rerank() is a
       pass-through for RERANKER=astradb (default).
    4. Generate answer with Mistral via WatsonX.
    5. Online eval (faithfulness + context_relevance).  If score < threshold:
       adaptive escalation (SOURCE_GAP → widen/expand/decompose;
       ANSWER_REPHRASE_GAP → re-generate with faithfulness directive).
       HTTP response is held until the best answer is ready (block-and-replace).
    6. Log to rag_eval_log.csv (includes escalation metadata if fired).
    """
    source = req.source_filter.upper() if req.source_filter else None
    if source and source not in {"SFC", "PCPD"}:
        raise HTTPException(
            status_code=422,
            detail=f"source_filter must be one of SFC, PCPD (got '{source}')",
        )

    # ── 1. Retrieve ───────────────────────────────────────────────────────────
    # top_k * 5 (capped at 40) gives the AstraDB NVIDIA reranker a wide pool.
    retrieve_n = min(req.top_k * 5, 40)

    try:
        candidates = retrieve(
            req.question,
            source_filter=source,
            top_n=retrieve_n,
            top_k=req.top_k,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Retrieval error: {exc}") from exc

    if not candidates:
        return ChatResponse(
            answer="No relevant regulatory content found for your question.",
            citations=[],
            model_used="",
            chunk_count=0,
        )

    # ── 2. COS full-doc fallback ──────────────────────────────────────────────
    # Appends wider Markdown passages from the original COS documents when the
    # best AstraDB chunk is below the rerank threshold.  Returns [] when USE_COS
    # is not set so local dev is unaffected.
    try:
        cos_extra = fetch_fallback_passages(candidates, max_extra=3)
    except Exception as exc:
        # COS errors must never kill the chat response — log and continue.
        import logging as _log
        _log.getLogger(__name__).warning("cos_retriever error (skipped): %s", exc)
        cos_extra = []

    # ── 3. Rerank ─────────────────────────────────────────────────────────────
    try:
        top_chunks = rerank(req.question, candidates, top_k=req.top_k)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Rerank error: {exc}") from exc

    # Append COS fallback passages after reranked AstraDB chunks so they act
    # as supplemental context rather than competing for the top slots.
    all_chunks = top_chunks + cos_extra

    # ── 4. Generate ───────────────────────────────────────────────────────────
    try:
        result = generate_answer(req.question, all_chunks)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Generation error: {exc}") from exc

    answer  = result["answer"]
    final_chunks = all_chunks

    # ── 5. Online eval + block-and-replace escalation ─────────────────────────
    eval_result = evaluate(req.question, final_chunks, answer)

    if not eval_result.passed:
        try:
            final_chunks, result, eval_result = escalate(
                req.question,
                final_chunks,
                answer,
                eval_result,
                source_filter=source,
                top_k=req.top_k,
                top_n=retrieve_n,
            )
            answer = result["answer"]
        except Exception as exc:
            # Escalation failure must never suppress the original answer.
            import logging as _log
            _log.getLogger(__name__).warning(
                "escalation failed, returning original answer: %s", exc
            )

    # ── 6. Log ────────────────────────────────────────────────────────────────
    log_request(
        question=req.question,
        chunks=final_chunks,
        answer=answer,
        gap_type=eval_result.gap_type,
        pre_score=_composite_score(evaluate(req.question, all_chunks, result.get("answer", answer)).scores)
            if eval_result.gap_type else None,
        post_score=_composite_score(eval_result.scores) if eval_result.gap_type else None,
    )

    return ChatResponse(
        answer=answer,
        citations=[CitationOut(**c) for c in result.get("citations", [])],
        model_used=result.get("model_used", ""),
        chunk_count=result.get("chunk_count", len(final_chunks)),
        eval_passed=eval_result.passed,
        gap_type=eval_result.gap_type,
        context_relevance=round(eval_result.scores["context_relevance"], 3),
        faithfulness=round(eval_result.scores["faithfulness"], 3),
    )


def _composite_score(scores: dict) -> float:
    return round(
        (scores.get("context_relevance", 0.0) + scores.get("faithfulness", 0.0)) / 2.0,
        3,
    )
