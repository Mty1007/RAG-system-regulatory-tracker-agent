"""Chat API router — POST /chat

Wires together retriever → reranker → generator into a single endpoint.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.generator import generate_answer
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
        default=10,
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

    1. Run hybrid (ANN + BM25 keyword) search on AstraDB via find_and_rerank().
    2. NVIDIA reranker trims candidates to top_k inside AstraDB.
    3. Generate a grounded answer with Mistral via WatsonX.
    """
    source = req.source_filter.upper() if req.source_filter else None
    if source and source not in {"SFC", "PCPD"}:
        raise HTTPException(
            status_code=422,
            detail=f"source_filter must be one of SFC, PCPD (got '{source}')",
        )

    # Retrieve more candidates (top_n) than the final count (top_k) so the
    # NVIDIA reranker inside find_and_rerank() has room to work, then returns
    # only top_k results — already sorted by rerank score.
    # top_k * 5 (max 40) gives the reranker a wider candidate pool, which
    # improves context relevance scores without changing the LLM call at all.
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

    log_request(question=req.question, chunks=top_chunks, answer=result["answer"])

    return ChatResponse(
        answer=result["answer"],
        citations=[CitationOut(**c) for c in result["citations"]],
        model_used=result["model_used"],
        chunk_count=result["chunk_count"],
    )
