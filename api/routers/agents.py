"""Agent skill endpoints for watsonx Orchestrate.

Three focused endpoints matching the 3-agent graph architecture:

    POST /agent/plan       — Planning Agent: detect source + search strategy
    POST /agent/retrieval  — Retrieval Agent: AstraDB retrieval + generate + evaluate
    POST /agent/cos        — COS Agent: COS fallback when retrieval fails evaluation

Each endpoint is a thin wrapper around the corresponding LangGraph node.
POST /chat remains the primary orchestrator that runs all three via the graph.
These endpoints exist so Orchestrate (or any client) can call each agent
individually as a tool.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.agents.nodes import (
    cos_node,
    planning_node,
    retrieval_node,
)
from api.routers.chat import _format_citation

logger = logging.getLogger(__name__)

router = APIRouter()


# ── /agent/plan ───────────────────────────────────────────────────────────────

class PlanRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000,
                          description="The user's regulatory question.")
    source_filter: Optional[str] = Field(
        default=None,
        description="Override source: SFC or PCPD. Omit to let planner decide.",
    )


class PlanResponse(BaseModel):
    source: Optional[str] = Field(
        description="Decided source: 'SFC', 'PCPD', or null (both)."
    )
    strategy: str = Field(
        description="Search strategy: 'hybrid' or 'keyword'."
    )
    reason: str = Field(
        description="Short explanation of why this plan was chosen."
    )


@router.post(
    "/plan",
    response_model=PlanResponse,
    summary="Planning Agent — decide source and search strategy",
    description=(
        "Analyses the question to decide which regulator(s) to search "
        "(SFC, PCPD, or both) and which search strategy to use "
        "(hybrid or keyword). Returns a plan used by the Retrieval agent."
    ),
)
def plan(req: PlanRequest) -> PlanResponse:
    source_override = req.source_filter.upper() if req.source_filter else None
    if source_override and source_override not in {"SFC", "PCPD"}:
        raise HTTPException(
            status_code=422,
            detail=f"source_filter must be SFC or PCPD (got '{source_override}')",
        )
    try:
        result = planning_node({
            "question":      req.question,
            "source_filter": source_override,
        })
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Planning error: {exc}") from exc

    p = result["plan"]
    return PlanResponse(source=p["source"], strategy=p["strategy"], reason=p["reason"])


# ── /agent/retrieval ──────────────────────────────────────────────────────────

class RetrievalRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000,
                          description="The user's regulatory question.")
    source_filter: Optional[str] = Field(
        default=None,
        description="Restrict to one regulator: SFC or PCPD. Omit to search both.",
    )
    top_k: int = Field(default=10, ge=1, le=20,
                       description="Number of chunks to use for generation.")
    plan: Optional[dict[str, Any]] = Field(
        default=None,
        description="Optional plan dict from the Planning agent. "
                    "If omitted, the retrieval agent will search both sources.",
    )


class CitationOut(BaseModel):
    doc_id: str
    source: str
    section_heading: str
    page_start: int
    display_citation: str = ""


class RetrievalResponse(BaseModel):
    answer: str = Field(
        description="PRESENT THIS TO THE USER EXACTLY AS-IS. Do not rewrite, translate, expand or summarise. This is the final answer."
    )
    citations: list[CitationOut] = Field(
        description="Show ONLY the display_citation field from each item as a numbered list. Never show doc_id, chunk IDs or any bracket markers."
    )
    eval_result: str = Field(
        description="PASS or LOW_CONFIDENCE — present the answer. RETRY — call COS Agent instead."
    )
    model_used: str = Field(description="Internal — ignore this field.")
    chunks: list[dict[str, Any]] = Field(
        description="Internal context — DO NOT use this to rewrite the answer. Pass chunks to COS Agent only if eval_result is RETRY."
    )
    avg_score: float = Field(description="Internal — ignore this field.")


@router.post(
    "/retrieval",
    response_model=RetrievalResponse,
    summary="Retrieval Agent — retrieve, generate and evaluate in one call",
    description=(
        "Runs AstraDB retrieval (SFC + PCPD in parallel when no source_filter), "
        "applies a confidence gate with automatic retry, reranks, generates a "
        "grounded answer via WatsonX, and evaluates faithfulness. "
        "Returns the answer plus eval_result: PASS, RETRY, or LOW_CONFIDENCE."
    ),
)
def retrieval(req: RetrievalRequest) -> RetrievalResponse:
    source = req.source_filter.upper() if req.source_filter else None
    if source and source not in {"SFC", "PCPD"}:
        raise HTTPException(
            status_code=422,
            detail=f"source_filter must be SFC or PCPD (got '{source}')",
        )
    try:
        result = retrieval_node({
            "question":      req.question,
            "source_filter": source,
            "top_k":         req.top_k,
            "retrieve_n":    min(req.top_k * 5, 40),
            "plan":          req.plan or {},
            "cos_fallback":  False,
            "retry_count":   0,
        })
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Retrieval error: {exc}") from exc

    return RetrievalResponse(
        answer=result["answer"],
        citations=[
            CitationOut(
                **c,
                display_citation=_format_citation(
                    c.get("doc_id", ""),
                    c.get("source", ""),
                    c.get("section_heading", ""),
                    c.get("page_start", 0),
                ),
            )
            for c in result.get("citations", [])
        ],
        model_used=result.get("model_used", ""),
        chunks=result.get("chunks", []),
        avg_score=result.get("avg_score", 0.0),
        eval_result=result.get("eval_result", "LOW_CONFIDENCE"),
    )


# ── /agent/cos ────────────────────────────────────────────────────────────────

class COSRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000,
                          description="The user's regulatory question.")
    chunks: list[dict[str, Any]] = Field(
        description="Current low-quality chunks from the Retrieval agent."
    )
    top_k: int = Field(default=10, ge=1, le=20,
                       description="Max chunks to return after COS enrichment.")


class COSResponse(BaseModel):
    answer: str = Field(description="Re-generated answer using COS-enriched context.")
    citations: list[CitationOut] = Field(description="Source citations for the answer.")
    model_used: str = Field(description="LLM model ID used for generation.")
    chunks: list[dict[str, Any]] = Field(
        description="Enriched chunks after fetching full markdown from COS."
    )
    eval_result: str = Field(
        description="PASS or LOW_CONFIDENCE after COS re-evaluation."
    )
    cos_fallback: bool = Field(
        description="Always True — confirms COS fallback ran."
    )


@router.post(
    "/cos",
    response_model=COSResponse,
    summary="COS Agent — fetch richer context and regenerate answer",
    description=(
        "When the Retrieval agent returns eval_result=RETRY, call this agent. "
        "It fetches the full markdown of the relevant documents from IBM COS, "
        "re-chunks them, merges with existing chunks, re-generates the answer, "
        "and re-evaluates faithfulness. Returns PASS or LOW_CONFIDENCE."
    ),
)
def cos(req: COSRequest) -> COSResponse:
    if not req.chunks:
        raise HTTPException(status_code=422, detail="chunks must not be empty")
    try:
        result = cos_node({
            "question":    req.question,
            "chunks":      req.chunks,
            "top_k":       req.top_k,
            "cos_fallback": False,
        })
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"COS error: {exc}") from exc

    return COSResponse(
        answer=result.get("answer", ""),
        citations=[
            CitationOut(
                **c,
                display_citation=_format_citation(
                    c.get("doc_id", ""),
                    c.get("source", ""),
                    c.get("section_heading", ""),
                    c.get("page_start", 0),
                ),
            )
            for c in result.get("citations", [])
        ],
        model_used=result.get("model_used", ""),
        chunks=result.get("chunks", req.chunks),
        eval_result=result.get("eval_result", "LOW_CONFIDENCE"),
        cos_fallback=result.get("cos_fallback", True),
    )
