"""Chat API router — POST /chat

Orchestrates the regulatory RAG pipeline via the LangGraph agent graph
(Phase 3).  The graph handles:

  research node  — hybrid retrieval (parallel SFC+PCPD, Phase 2) +
                   confidence gate retry (Phase 1)
  generate node  — WatsonX Mistral answer generation
  evaluate node  — faithfulness check; RETRY loops back to research
                   (max 1 retry), PASS / LOW_CONFIDENCE ends

Helper functions (_merge_chunks, _retrieve_candidates, _avg_rerank_score)
are kept here so the nodes in core/agents/nodes.py can import them without
circular dependencies, and so the existing unit tests continue to work.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.agents.graph import build_graph
from core.retriever import retrieve

logger = logging.getLogger(__name__)

router = APIRouter()

# Compiled LangGraph — built once at import time.
_graph = build_graph()

# Avg rerank score below which a wider-pool retry is triggered.
# Mirrors the threshold in core/agents/nodes.py.
_CONFIDENCE_THRESHOLD = 0.30


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


# ── doc_id → human-readable title mapping ────────────────────────────────────
# doc_ids are minted as "<source_lower>-<sha1[:12]>".  We maintain a short
# lookup of known doc_ids → short titles for the most-cited documents so that
# citations shown in Orchestrate are meaningful to users.
# Unknown doc_ids fall back to a formatted source + id string.
_DOC_TITLES: dict[str, str] = {
    # SFC licensing & conduct
    "sfc-fa3313ebef36": "SFC Licensing Handbook",
    "sfc-7ad2b7b10d05": "SFC Licensing Handbook — Experience & Qualifications",
    "sfc-a90505b192cd": "SFC Internal Control Guidelines",
    "sfc-46ae232f098a": "SFC Fit and Proper Guidelines (Jan 2022)",
    "sfc-c6c3ef4777ac": "SFC Guidelines for Virtual Asset Trading Platform Operators (Jun 2023)",
    "sfc-c877cc7924ca": "SFC AML/CFT Guideline for Associated Entities & VA Service Providers",
    "sfc-f3409000cced": "SFC Handbook for Unit Trusts and Mutual Funds",
    # PCPD personal data
    "pcpd-488f6350b910": "PCPD Introduction to the Personal Data (Privacy) Ordinance",
    "pcpd-5304051db39f": "PCPD Guidance Note on Data Security Measures for ICT",
    "pcpd-44301d3dfee8": "PCPD Guidance on Data Breach Handling",
    # SFC additional documents
    "sfc-0c5e498d3aa4": "SFC Code of Conduct — Overarching Principles",
    "sfc-4dca9266048c": "SFC Code of Conduct for Licensed and Registered Persons",
    "sfc-2caebfc094e4": "SFC Code on Unit Trusts and Mutual Funds",
    "sfc-3338df413fd6": "SFC Licensing Handbook (Fifth Edition, Oct 2024)",
    "sfc-52d12e5f7b28": "SFC Circular on Debt Collection by Licensed Corporations",
    "sfc-8011bfdac130": "SFC Handbook for Unit Trusts, ILAs and Unlisted Structured Products",
    "sfc-fa36b24cad6a": "SFC Guideline on Anti-Money Laundering and Counter-Financing of Terrorism (Jul 2019)",
    "sfc-9a13d933e8f0": "SFC Licensing Handbook — VA Platform Operators & Representatives",
    "sfc-efef77b0e345": "SFC Guidelines on Regulation of Automated Trading Services (ATS)",
}

_SOURCE_LABELS = {"SFC": "Securities and Futures Commission (SFC)",
                  "PCPD": "Privacy Commissioner for Personal Data (PCPD)"}


def _format_citation(doc_id: str, source: str, section_heading: str, page_start: int) -> str:
    """Return a human-readable citation string for display in Orchestrate."""
    title = _DOC_TITLES.get(doc_id) or f"{source} Regulatory Document"
    regulator = _SOURCE_LABELS.get(source, source)
    parts = [f"{regulator} — {title}"]
    if section_heading and section_heading != "—":
        clean_heading = section_heading.replace("§", "Section ")
        parts.append(f"Section: {clean_heading}")
    if page_start and page_start > 0:
        parts.append(f"p. {page_start}")
    return " | ".join(parts)


class CitationOut(BaseModel):
    doc_id: str
    source: str
    section_heading: str
    page_start: int
    display_citation: str = ""


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

def _merge_chunks(lists: list[list[dict]], top_k: int) -> list[dict]:
    """Merge multiple chunk lists, dedup by _id keeping highest rerank_score,
    and return the top_k chunks sorted by descending rerank_score."""
    seen: dict[str, dict] = {}
    for chunks in lists:
        for c in chunks:
            cid = c["_id"]
            if cid not in seen or c.get("rerank_score", 0.0) > seen[cid].get("rerank_score", 0.0):
                seen[cid] = c
    return sorted(seen.values(), key=lambda x: x.get("rerank_score", 0.0), reverse=True)[:top_k]


def _retrieve_candidates(
    question: str,
    source: Optional[str],
    retrieve_n: int,
    top_k: int,
) -> list[dict]:
    """Run retrieval for *question*.

    When *source* is set, runs a single retrieve() call for that source.
    When *source* is None, fans out two concurrent retrieve() calls — one
    for SFC and one for PCPD — and merges the results.  This halves the
    wall-clock retrieval time for cross-regulator questions.
    """
    if source:
        return retrieve(question, source_filter=source, top_n=retrieve_n, top_k=top_k)

    results: dict[str, list[dict]] = {}
    errors:  list[str] = []

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(retrieve, question, source_filter=s, top_n=retrieve_n, top_k=top_k): s
            for s in ("SFC", "PCPD")
        }
        for future in as_completed(futures):
            src = futures[future]
            try:
                results[src] = future.result()
            except Exception as exc:
                errors.append(f"{src}: {exc}")
                logger.warning("parallel retrieve: %s failed — %s", src, exc)

    if errors and not results:
        raise RuntimeError(f"All parallel retrieval calls failed: {'; '.join(errors)}")

    merged = _merge_chunks(list(results.values()), top_k)
    logger.info(
        "parallel retrieve: SFC=%d PCPD=%d → merged=%d",
        len(results.get("SFC", [])),
        len(results.get("PCPD", [])),
        len(merged),
    )
    return merged


def _avg_rerank_score(chunks: list[dict]) -> float:
    """Return the average rerank score of *chunks*, or 0.0 if empty."""
    if not chunks:
        return 0.0
    return sum(c.get("rerank_score", 0.0) for c in chunks) / len(chunks)


@router.post("/", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """Answer a regulatory question via the LangGraph RAG agent.

    The graph runs: research → generate → evaluate, with one automatic
    retry cycle if the evaluator flags low faithfulness.
    """
    source = req.source_filter.upper() if req.source_filter else None
    if source and source not in {"SFC", "PCPD"}:
        raise HTTPException(
            status_code=422,
            detail=f"source_filter must be one of SFC, PCPD (got '{source}')",
        )

    retrieve_n = min(req.top_k * 5, 40)

    try:
        state = _graph.invoke({
            "question":      req.question,
            "source_filter": source,
            "top_k":         req.top_k,
            "retrieve_n":    retrieve_n,
            "retry_count":   0,
            "cos_fallback":  False,
        })
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Agent error: {exc}") from exc

    answer = state.get("answer", "")
    if not answer:
        return ChatResponse(
            answer="No relevant regulatory content found for your question.",
            citations=[],
            model_used="",
            chunk_count=0,
            eval_passed=False,
        )

    citations = state.get("citations", [])
    eval_result = state.get("eval_result", "PASS")
    return ChatResponse(
        answer=answer,
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
            for c in citations
        ],
        model_used=state.get("model_used", ""),
        chunk_count=len(state.get("chunks", [])),
        eval_passed=eval_result == "PASS",
        gap_type=None if eval_result == "PASS" else eval_result,
    )
