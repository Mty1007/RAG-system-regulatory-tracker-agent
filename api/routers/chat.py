"""Chat API router — POST /chat

Wires together retriever → reranker → generator into a single endpoint.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.retriever import retrieve
from core.reranker import rerank
from core.generator import generate_answer

router = APIRouter()


# ── request / response schemas ────────────────────────────────────────────────

class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    source_filter: Optional[str] = Field(
        default=None,
        description="Restrict retrieval to one regulator: SFC, IA, or PCPD. "
                    "Omit or pass null to search all sources.",
    )
    top_k: int = Field(
        default=5,
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


# ── endpoint ──────────────────────────────────────────────────────────────────

@router.post("/", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """Answer a regulatory question using hybrid RAG.

    1. Embed the question and run hybrid (ANN + keyword) search on AstraDB.
    2. Rerank the top-N candidates (WatsonX or local cross-encoder).
    3. Generate a grounded answer with IBM Granite via WatsonX.
    """
    source = req.source_filter.upper() if req.source_filter else None
    if source and source not in {"SFC", "IA", "PCPD"}:
        raise HTTPException(
            status_code=422,
            detail=f"source_filter must be one of SFC, IA, PCPD (got '{source}')",
        )

    # retrieve more candidates than top_k so the reranker has room to work
    retrieve_n = min(req.top_k * 4, 20)

    try:
        candidates = retrieve(
            req.question,
            source_filter=source,
            top_n=retrieve_n,
            top_k=retrieve_n,
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

    try:
        top_chunks = rerank(req.question, candidates, top_k=req.top_k)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Rerank error: {exc}") from exc

    try:
        result = generate_answer(req.question, top_chunks)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Generation error: {exc}"
        ) from exc

    return ChatResponse(
        answer=result["answer"],
        citations=[CitationOut(**c) for c in result["citations"]],
        model_used=result["model_used"],
        chunk_count=result["chunk_count"],
    )
