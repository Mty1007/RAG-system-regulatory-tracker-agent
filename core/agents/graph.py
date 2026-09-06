"""LangGraph agent graph for the regulatory RAG pipeline.

Graph structure
---------------

    [planning] → [retrieval] → PASS/LOW_CONFIDENCE → END
                     |
                   RETRY
                     ↓
                   [cos] → END (PASS or LOW_CONFIDENCE)

Three agents
------------
planning_node   — decides source (SFC/PCPD/both) + search strategy
retrieval_node  — retrieves from AstraDB, generates answer, evaluates
                  faithfulness; returns eval_result
cos_node        — COS fallback: fetches full markdown, enriches context,
                  re-generates and re-evaluates; always ends after this

Routing logic
-------------
retrieval eval_result == "RETRY"          → cos_node
retrieval eval_result == "PASS"           → END
retrieval eval_result == "LOW_CONFIDENCE" → END

cos_node always → END  (no further retries after COS)

Usage
-----
    from core.agents.graph import build_graph

    graph = build_graph()
    result = graph.invoke({
        "question":      "What are the SFC licensing requirements?",
        "source_filter": None,
        "top_k":         10,
        "retrieve_n":    40,
        "retry_count":   0,
        "cos_fallback":  False,
    })
    # result["answer"], result["citations"], result["eval_result"]
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph

from core.agents.nodes import (
    cos_node,
    planning_node,
    retrieval_node,
)
from core.agents.state import RAGState

logger = logging.getLogger(__name__)


def _route_after_retrieval(
    state: RAGState,
) -> Literal["cos", "__end__"]:
    """Conditional edge after retrieval_node.

    RETRY → cos_node  (retrieval failed faithfulness check — try COS)
    PASS / LOW_CONFIDENCE → END
    """
    eval_result = state.get("eval_result", "PASS")

    if eval_result == "RETRY":
        logger.info("graph: routing RETRY → cos_node")
        return "cos"

    return END


def build_graph() -> StateGraph:
    """Build and compile the 3-agent RAG graph.

    Returns a compiled LangGraph that can be invoked with .invoke()
    or .stream() directly.
    """
    builder = StateGraph(RAGState)

    # ── nodes ─────────────────────────────────────────────────────────────────
    builder.add_node("planning",   planning_node)
    builder.add_node("retrieval",  retrieval_node)
    builder.add_node("cos",        cos_node)

    # ── edges ─────────────────────────────────────────────────────────────────
    builder.add_edge(START,       "planning")
    builder.add_edge("planning",  "retrieval")

    # After retrieval: RETRY → cos, else END
    builder.add_conditional_edges(
        "retrieval",
        _route_after_retrieval,
        {"cos": "cos", END: END},
    )

    # cos always ends (no further retries)
    builder.add_edge("cos", END)

    return builder.compile()
