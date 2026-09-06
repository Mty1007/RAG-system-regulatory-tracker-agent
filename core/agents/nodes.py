"""LangGraph node functions for the regulatory RAG agent.

Each function takes a RAGState dict, does its job using the existing
core/ functions, and returns a partial RAGState dict with only the
fields it produced.  LangGraph merges the returned dict into the
running state automatically.

Nodes
-----
planning_node   — detects source (SFC/PCPD/both) + search strategy
retrieval_node  — AstraDB retrieval + confidence gate + generate + evaluate
                  (all-in-one: replaces the old research/generate/evaluate trio)
cos_node        — COS fallback: fetches full markdown for failed doc_ids,
                  re-chunks, re-generates, and re-evaluates

Nothing in core/ is changed — these nodes are thin wrappers only.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from core.agents.state import RAGState
from core.generator import generate_answer
from core.rag_eval import log_request
from core.reranker import rerank

logger = logging.getLogger(__name__)

# Avg rerank score below which a wider-pool retry is triggered inside
# the retrieval node.
_CONFIDENCE_THRESHOLD = 0.30

# Faithfulness check floor — fraction of answer tokens that must appear
# in the combined chunk texts.
_FAITHFULNESS_FLOOR = 0.10

# ── SFC/PCPD keyword patterns for planning ────────────────────────────────────
_SFC_RE  = re.compile(
    r"\bSFC\b|securities.*futures|licensed.corp|asset.manag|type [0-9]+ "
    r"regulated|fit.and.proper|AML|anti.money|internal.control|conduct",
    re.IGNORECASE,
)
_PCPD_RE = re.compile(
    r"\bPCPD\b|personal.data|data.protect|privacy|data.breach|PDPO|"
    r"data.subject|data.user",
    re.IGNORECASE,
)
# Keyword-heavy queries benefit from a keyword-biased strategy
_KEYWORD_RE = re.compile(
    r"section|clause|paragraph|article|chapter|schedule|ordinance|"
    r"cap\.\s*\d+|regulation\s+\d+",
    re.IGNORECASE,
)


def _avg_score(chunks: list[dict[str, Any]]) -> float:
    if not chunks:
        return 0.0
    return sum(c.get("rerank_score", 0.0) for c in chunks) / len(chunks)


def _faithfulness_check(answer: str, chunks: list[dict[str, Any]]) -> float:
    """Return token-overlap ratio of answer vs combined chunk texts."""
    answer_tokens  = set(re.findall(r"\w+", answer.lower()))
    context_tokens = set(
        tok
        for c in chunks
        for tok in re.findall(r"\w+", c.get("text", "").lower())
    )
    if not answer_tokens:
        return 0.0
    return len(answer_tokens & context_tokens) / len(answer_tokens)


# ── planning_node ─────────────────────────────────────────────────────────────

def planning_node(state: RAGState) -> dict:
    """Analyse the question and produce a retrieval plan.

    Decides:
    - source:   "SFC" | "PCPD" | None (both)
    - strategy: "hybrid" | "semantic" | "keyword"

    Rules
    -----
    - If question explicitly mentions SFC terms only → source=SFC
    - If question explicitly mentions PCPD terms only → source=PCPD
    - If both or neither → source=None (search both)
    - If question contains clause/section/schedule references → keyword
    - Otherwise → hybrid (default)

    Falls back to source=None, strategy=hybrid on any error so the
    pipeline always continues.
    """
    question = state.get("question", "")
    source_override = state.get("source_filter")  # respect explicit caller override

    try:
        has_sfc  = bool(_SFC_RE.search(question))
        has_pcpd = bool(_PCPD_RE.search(question))

        if source_override:
            source = source_override.upper()
        elif has_sfc and not has_pcpd:
            source = "SFC"
        elif has_pcpd and not has_sfc:
            source = "PCPD"
        else:
            source = None  # search both

        strategy = "keyword" if _KEYWORD_RE.search(question) else "hybrid"

        reason = (
            f"source={'both' if source is None else source} "
            f"strategy={strategy} "
            f"sfc_keywords={has_sfc} pcpd_keywords={has_pcpd}"
        )

        plan = {"source": source, "strategy": strategy, "reason": reason}

    except Exception as exc:
        logger.warning("planning_node: failed (%s) — using defaults", exc)
        plan = {"source": None, "strategy": "hybrid", "reason": "fallback"}

    logger.info("planning_node: %s", plan["reason"])
    return {"plan": plan}


# ── retrieval_node ────────────────────────────────────────────────────────────

def retrieval_node(state: RAGState) -> dict:
    """Retrieve chunks from AstraDB, generate an answer, and evaluate it.

    This is the core agent node — it combines what were previously three
    separate nodes (research, generate, evaluate) into one cohesive unit:

    1. Retrieve candidates from AstraDB (parallel SFC + PCPD fan-out)
    2. Confidence gate: retry with wider pool if avg rerank score < 0.30
    3. Rerank to top_k
    4. Generate answer via WatsonX
    5. Evaluate faithfulness (token-overlap)
    6. Return eval_result so the graph can route to COS fallback or END

    Returns
    -------
    chunks, avg_score, answer, citations, model_used, eval_result,
    retry_count
    """
    from api.routers.chat import _retrieve_candidates

    question     = state["question"]
    plan         = state.get("plan", {})
    source       = plan.get("source") or state.get("source_filter")
    retrieve_n   = state.get("retrieve_n", 40)
    top_k        = state.get("top_k", 10)
    cos_fallback = state.get("cos_fallback", False)

    # ── 1. Retrieve ───────────────────────────────────────────────────────────
    candidates = _retrieve_candidates(
        question=question,
        source=source,
        retrieve_n=retrieve_n,
        top_k=top_k,
    )

    # ── 2. Confidence gate ────────────────────────────────────────────────────
    initial_score = _avg_score(candidates)
    if initial_score < _CONFIDENCE_THRESHOLD:
        logger.warning(
            "retrieval_node: low confidence (avg_rerank=%.3f) — retrying wider pool",
            initial_score,
        )
        try:
            retry_n = min(retrieve_n * 2, 80)
            retry_candidates = _retrieve_candidates(
                question=question,
                source=source,
                retrieve_n=retry_n,
                top_k=top_k,
            )
            if _avg_score(retry_candidates) > initial_score:
                candidates = retry_candidates
        except Exception as exc:
            logger.warning("retrieval_node: retry failed (%s), using original", exc)

    # ── 3. Rerank ─────────────────────────────────────────────────────────────
    top_chunks = rerank(question, candidates, top_k=top_k)
    avg = _avg_score(top_chunks)
    logger.info("retrieval_node: %d chunks, avg_rerank=%.3f", len(top_chunks), avg)

    # ── 4. Generate ───────────────────────────────────────────────────────────
    gen_result = generate_answer(question, top_chunks)
    answer     = gen_result["answer"]
    citations  = gen_result["citations"]
    model_used = gen_result["model_used"]
    logger.info("retrieval_node: model=%s  answer_len=%d", model_used, len(answer))

    # ── 5. Evaluate faithfulness ──────────────────────────────────────────────
    log_request(question=question, chunks=top_chunks, answer=answer)

    if not answer:
        eval_result = "LOW_CONFIDENCE"
    else:
        overlap = _faithfulness_check(answer, top_chunks)
        if overlap >= _FAITHFULNESS_FLOOR:
            eval_result = "PASS"
        elif cos_fallback:
            eval_result = "LOW_CONFIDENCE"
            logger.warning(
                "retrieval_node: low faithfulness overlap=%.2f after COS — LOW_CONFIDENCE",
                overlap,
            )
        else:
            eval_result = "RETRY"
            logger.warning(
                "retrieval_node: low faithfulness overlap=%.2f — flagging RETRY", overlap
            )

    logger.info("retrieval_node: eval_result=%s", eval_result)

    return {
        "chunks":      top_chunks,
        "avg_score":   avg,
        "answer":      answer,
        "citations":   citations,
        "model_used":  model_used,
        "eval_result": eval_result,
        "retry_count": state.get("retry_count", 0) + 1,
    }


# ── cos_node ──────────────────────────────────────────────────────────────────

def cos_node(state: RAGState) -> dict:
    """COS fallback — fetch full markdown for the doc_ids from failed chunks.

    When the retrieval_node returns RETRY, this node is invoked.  It:
    1. Extracts unique doc_ids from the current (low-quality) chunks
    2. Downloads the full markdown for each doc from COS
    3. Re-chunks the markdown using core/chunker.chunk_markdown
    4. Merges the new chunks with the existing ones (dedup by chunk_id)
    5. Scores new chunks by keyword overlap with the question
    6. Re-generates the answer with the enriched context
    7. Re-evaluates faithfulness and returns eval_result

    This gives the generator access to the full document text rather than
    just the pre-indexed fixed-size chunks — useful when the answer spans
    multiple sections that weren't co-located in a single chunk.
    """
    import os
    import ibm_boto3
    from ibm_botocore.client import Config
    from core.chunker import chunk_markdown

    question = state["question"]
    chunks   = state.get("chunks", [])
    top_k    = state.get("top_k", 10)

    # Extract unique doc_ids from current chunks
    doc_ids = list({c.get("doc_id", "") for c in chunks if c.get("doc_id")})
    if not doc_ids:
        logger.warning("cos_node: no doc_ids in current chunks — skipping COS fallback")
        return {"cos_fallback": True, "eval_result": "LOW_CONFIDENCE"}

    # Build COS client
    try:
        cos = ibm_boto3.client(
            "s3",
            ibm_api_key_id=os.environ["COS_API_KEY"],
            ibm_service_instance_id=os.environ["COS_INSTANCE_CRN"],
            config=Config(signature_version="oauth"),
            endpoint_url=os.environ["COS_ENDPOINT"],
        )
        bucket = os.environ["COS_BUCKET"]
    except Exception as exc:
        logger.warning("cos_node: COS client init failed (%s) — skipping", exc)
        return {"cos_fallback": True, "eval_result": "LOW_CONFIDENCE"}

    # Fetch markdown + re-chunk for each doc_id
    new_chunks: list[dict] = []
    for doc_id in doc_ids[:3]:  # limit to top 3 docs to keep latency reasonable
        try:
            key = f"transformed/{doc_id}.md"
            obj = cos.get_object(Bucket=bucket, Key=key)
            markdown = obj["Body"].read().decode("utf-8")
            source = doc_id.split("-")[0].upper()

            rechunked = chunk_markdown(doc_id, markdown)
            for c in rechunked:
                c["source"]       = source
                c["page_start"]   = 0
                c["rerank_score"] = 0.0
                c["rrf_score"]    = 0.0
            new_chunks.extend(rechunked)
            logger.info("cos_node: re-chunked %s → %d chunks", doc_id, len(rechunked))
        except Exception as exc:
            logger.warning("cos_node: could not fetch %s from COS (%s)", doc_id, exc)

    if not new_chunks:
        logger.warning("cos_node: no new chunks from COS — using original context")
        # Re-generate + re-evaluate with original chunks as last-resort
        enriched = chunks
    else:
        # Merge new chunks with existing, dedup, score by keyword overlap
        existing_ids = {c.get("_id", c.get("chunk_id", "")) for c in chunks}
        unique_new   = [c for c in new_chunks if c.get("chunk_id", "") not in existing_ids]

        q_tokens = set(re.findall(r"\w+", question.lower()))
        for c in unique_new:
            c_tokens = set(re.findall(r"\w+", c.get("text", "").lower()))
            overlap  = len(q_tokens & c_tokens) / max(len(q_tokens), 1)
            c["rerank_score"] = overlap

        enriched = sorted(
            chunks + unique_new,
            key=lambda x: x.get("rerank_score", 0.0),
            reverse=True,
        )[:top_k]

        logger.info(
            "cos_node: merged %d original + %d new COS chunks → %d total",
            len(chunks), len(unique_new), len(enriched),
        )

    # Re-generate with enriched context
    gen_result = generate_answer(question, enriched)
    answer     = gen_result["answer"]
    citations  = gen_result["citations"]
    model_used = gen_result["model_used"]
    logger.info("cos_node: regenerated answer_len=%d", len(answer))

    # Re-evaluate faithfulness (final check — no further retries after COS)
    log_request(question=question, chunks=enriched, answer=answer)
    overlap     = _faithfulness_check(answer, enriched)
    eval_result = "PASS" if overlap >= _FAITHFULNESS_FLOOR else "LOW_CONFIDENCE"
    logger.info("cos_node: eval_result=%s  overlap=%.2f", eval_result, overlap)

    return {
        "chunks":      enriched,
        "answer":      answer,
        "citations":   citations,
        "model_used":  model_used,
        "eval_result": eval_result,
        "cos_fallback": True,
    }
