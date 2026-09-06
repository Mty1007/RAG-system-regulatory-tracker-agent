"""LangGraph state definition for the regulatory RAG agent.

The RAGState TypedDict is the single shared object passed between every
node in the graph.  Each node reads what it needs and writes back its
output — no global variables, no side-channel passing.

Fields
------
question        Original user question (set at graph entry, never changed).
source_filter   Optional "SFC" | "PCPD" — None means search both.
top_k           Number of chunks to pass to the LLM.
retrieve_n      Candidate pool size for AstraDB retrieval.

plan            Dict produced by planning_node:
                  {
                    "source":   "SFC" | "PCPD" | None,
                    "strategy": "hybrid" | "semantic" | "keyword",
                    "reason":   str
                  }

chunks          Reranked chunks returned by the research node.
avg_score       Avg rerank score of the chunks (set by research node).

answer          Generated answer text (set by generate node).
citations       List of citation dicts (set by generate node).
model_used      Model ID string (set by generate node).

eval_result     "PASS" | "RETRY" | "LOW_CONFIDENCE" (set by evaluate node).
retry_count     Number of research→generate→evaluate cycles attempted.

cos_fallback    True once the COS fallback node has already run — prevents
                infinite COS→evaluate loops.
"""

from __future__ import annotations

from typing import Any, Optional
from typing_extensions import TypedDict


class RAGState(TypedDict, total=False):
    # ── input ─────────────────────────────────────────────────────────────────
    question:       str
    source_filter:  Optional[str]
    top_k:          int
    retrieve_n:     int

    # ── planning node output ──────────────────────────────────────────────────
    plan:           dict[str, Any]

    # ── research node output ──────────────────────────────────────────────────
    chunks:         list[dict[str, Any]]
    avg_score:      float

    # ── generate node output ──────────────────────────────────────────────────
    answer:         str
    citations:      list[dict[str, Any]]
    model_used:     str

    # ── evaluate node output ──────────────────────────────────────────────────
    eval_result:    str   # "PASS" | "RETRY" | "LOW_CONFIDENCE"

    # ── orchestration ─────────────────────────────────────────────────────────
    retry_count:    int
    cos_fallback:   bool   # True once COS fallback has already run
