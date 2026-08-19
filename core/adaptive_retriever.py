"""Adaptive retriever — staged escalation for SOURCE_GAP and ANSWER_REPHRASE_GAP.

Called by api/routers/chat.py when core/online_eval.evaluate() returns a
non-passing EvalResult.  The caller passes the original question, chunks,
answer, and gap type; this module tries progressively harder fixes, re-scores
after each stage, and returns the best (question, chunks, answer) triple seen.

SOURCE_GAP escalation (3 stages)
---------------------------------
Stage 1 — Widen candidate pool
    Re-run retrieve() with top_n doubled (capped at 80).  Cheap: one extra
    AstraDB find_and_rerank() call.

Stage 2 — Query expansion on top of stage 1
    Call _expand_query() from core/retriever to produce a synonym-enriched
    query, then retrieve() again with the wider pool.  Costs one WatsonX call
    plus one AstraDB call.

Stage 3 — Sub-question decomposition
    Ask the LLM to split the question into ≤3 sub-questions; run a separate
    retrieve() for each; merge and deduplicate results by chunk _id, keeping
    the highest rerank_score for any duplicate.  Expensive: 1 WatsonX call
    + N AstraDB calls.  Only triggered if stages 1 and 2 both fail.

Each stage re-generates the answer with the new chunks and re-scores.
If the new score is strictly better than the current best, the result is
adopted.  If not, the previous best is retained (no regression).
Escalation stops at the first stage that produces a passing score.

ANSWER_REPHRASE_GAP escalation
--------------------------------
No new retrieval.  The original chunks are kept; generate_answer() is called
once with a faithfulness directive prepended to the system prompt.  One
WatsonX generation call.  Result is kept only if re-score is strictly better.

Required environment variables
-------------------------------
(Same as core/retriever and core/generator — no new vars required)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests

from core.embedder import _get_iam_token
from core.generator import generate_answer
from core.online_eval import EvalResult, evaluate
from core.retriever import _expand_query, retrieve

logger = logging.getLogger(__name__)

# Hard cap on top_n during widened retrieval — AstraDB supports up to 1000
# but 80 gives the NVIDIA reranker a meaningfully wider pool without ballooning
# latency.
_MAX_WIDENED_TOP_N = 80

# Maximum number of sub-questions produced during stage-3 decomposition.
_MAX_SUB_QUESTIONS = 3

# Prompt to rephrase the user question into alternative search vocabulary.
_REPHRASE_QUERY_PROMPT = (
    "You are a Hong Kong financial regulatory expert. "
    "The following question did not retrieve sufficiently relevant regulatory passages. "
    "Rewrite it using different vocabulary — use formal regulatory terminology, "
    "synonyms, and alternative phrasings that are more likely to appear in "
    "SFC and PCPD regulatory documents. "
    "Output ONLY the rewritten question as a single line, nothing else.\n\n"
    "Original question: {question}\n\nRewritten question:"
)


# ── sub-question decomposition (stage 3) ─────────────────────────────────────

def _decompose_question(question: str) -> list[str]:
    """Ask the LLM to split *question* into ≤_MAX_SUB_QUESTIONS sub-questions.

    Returns the original question in a list on any error so stage 3 degrades
    gracefully to a single-query retrieval.
    """
    try:
        api_key    = os.environ["WATSONX_API_KEY"]
        project_id = os.environ["WATSONX_PROJECT_ID"]
        base_url   = os.environ["WATSONX_URL"].rstrip("/")
        model_id   = os.environ.get("WATSONX_LLM_MODEL", "mistralai/mistral-medium-2505")
        token      = _get_iam_token(api_key)

        prompt = (
            f"Break the following regulatory question into at most "
            f"{_MAX_SUB_QUESTIONS} specific, self-contained sub-questions "
            f"that together cover the full answer. "
            f"Output ONLY a numbered list, one sub-question per line.\n\n"
            f"Question: {question}\n\nSub-questions:"
        )
        resp = requests.post(
            base_url + "/ml/v1/text/generation?version=2023-10-25",
            json={
                "model_id":   model_id,
                "project_id": project_id,
                "input":      prompt,
                "parameters": {
                    "decoding_method": "greedy",
                    "max_new_tokens":  200,
                    "stop_sequences":  ["\n\n"],
                },
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning(
                "adaptive_retriever: decompose HTTP %d", resp.status_code
            )
            return [question]

        raw = (
            resp.json().get("results", [{}])[0]
            .get("generated_text", "")
            .strip()
        )
        # Parse "1. …\n2. …\n3. …" format; strip numbering
        sub_qs = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            # Strip leading "1." / "1)" / "-" markers
            import re
            cleaned = re.sub(r"^[\d]+[.)]\s*|^[-•]\s*", "", line).strip()
            if cleaned:
                sub_qs.append(cleaned)

        if sub_qs:
            logger.info(
                "adaptive_retriever: decomposed into %d sub-questions", len(sub_qs)
            )
            return sub_qs[:_MAX_SUB_QUESTIONS]
    except Exception as exc:
        logger.warning("adaptive_retriever: decompose failed: %s", exc)

    return [question]


# ── question rephrasing (ANSWER_REPHRASE_GAP) ────────────────────────────────

def _rephrase_question(question: str) -> str:
    """Ask the LLM to rewrite *question* using alternative regulatory vocabulary.

    The rephrased question is used as the new search query so that different
    chunks are retrieved — the root cause of a rephrase gap is that the
    original phrasing did not surface the most relevant passages.

    Returns the original question unchanged on any error so the caller
    degrades gracefully.
    """
    try:
        api_key    = os.environ["WATSONX_API_KEY"]
        project_id = os.environ["WATSONX_PROJECT_ID"]
        base_url   = os.environ["WATSONX_URL"].rstrip("/")
        model_id   = os.environ.get("WATSONX_LLM_MODEL", "mistralai/mistral-medium-2505")
        token      = _get_iam_token(api_key)

        prompt = _REPHRASE_QUERY_PROMPT.format(question=question)
        resp = requests.post(
            base_url + "/ml/v1/text/generation?version=2023-10-25",
            json={
                "model_id":   model_id,
                "project_id": project_id,
                "input":      prompt,
                "parameters": {
                    "decoding_method": "greedy",
                    "max_new_tokens":  120,
                    "stop_sequences":  ["\n"],
                },
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning("adaptive_retriever: rephrase HTTP %d", resp.status_code)
            return question

        rephrased = (
            resp.json().get("results", [{}])[0]
            .get("generated_text", "")
            .strip()
        )
        if rephrased and rephrased != question:
            logger.info(
                "adaptive_retriever: rephrased query '%s' → '%s'",
                question[:60], rephrased[:60],
            )
            return rephrased
    except Exception as exc:
        logger.warning("adaptive_retriever: rephrase_question failed: %s", exc)

    return question


# ── merge helpers ─────────────────────────────────────────────────────────────

def _merge_chunks(
    base: list[dict[str, Any]],
    additions: list[dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    """Merge two chunk lists, deduplicate by _id, keep highest rerank_score."""
    seen: dict[str, dict[str, Any]] = {c["_id"]: c for c in base}
    for c in additions:
        cid = c["_id"]
        if cid not in seen or c.get("rerank_score", 0.0) > seen[cid].get("rerank_score", 0.0):
            seen[cid] = c
    return sorted(seen.values(), key=lambda x: x.get("rerank_score", 0.0), reverse=True)[:top_k]


# ── composite score helper ────────────────────────────────────────────────────

def _composite(scores: dict[str, float]) -> float:
    return (scores.get("context_relevance", 0.0) + scores.get("faithfulness", 0.0)) / 2.0


# ── public entry point ────────────────────────────────────────────────────────

def escalate(
    question: str,
    chunks: list[dict[str, Any]],
    answer: str,
    eval_result: EvalResult,
    *,
    source_filter: Optional[str] = None,
    top_k: int = 15,
    top_n: int = 20,
) -> tuple[list[dict[str, Any]], dict[str, Any], EvalResult]:
    """Attempt to improve quality by escalating retrieval or generation.

    Parameters
    ----------
    question:
        Original user question.
    chunks:
        Chunks that produced the failing answer.
    answer:
        The failing generated answer string.
    eval_result:
        EvalResult from the initial evaluate() call.
    source_filter:
        Passed through to retrieve() unchanged.
    top_k:
        Final number of chunks to keep after each retrieval stage.
    top_n:
        Candidate pool size for the original retrieve() call (used to
        compute the widened pool for stage 1).

    Returns
    -------
    (best_chunks, best_result_dict, best_eval)
        best_result_dict has the same shape as generate_answer() output.
        best_eval is the EvalResult for the returned answer.
    """
    gap = eval_result.gap_type
    threshold = eval_result.threshold

    best_chunks = chunks
    best_result: dict[str, Any] = {"answer": answer, "citations": [], "model_used": "", "chunk_count": len(chunks)}
    best_eval = eval_result
    best_score = _composite(eval_result.scores)

    logger.info("adaptive_retriever: escalating gap=%s  best_score=%.3f", gap, best_score)

    # ── ANSWER_REPHRASE_GAP — re-retrieve with rephrased search terms ─────────
    # The original phrasing did not surface sufficiently relevant chunks.
    # Rephrase the question into alternative regulatory vocabulary, re-retrieve
    # fresh chunks with the new search terms, then re-generate and re-score.
    if gap in ("ANSWER_REPHRASE_GAP", "BOTH"):
        try:
            rephrased_q = _rephrase_question(question)
            if rephrased_q != question:
                rp_chunks = retrieve(
                    rephrased_q,
                    source_filter=source_filter,
                    top_n=top_n,
                    top_k=top_k,
                )
                rp_chunks = _merge_chunks(chunks, rp_chunks, top_k)
                rp_result = generate_answer(question, rp_chunks)   # answer in original question language
                rp_eval   = evaluate(question, rp_chunks, rp_result["answer"])
                rp_score  = _composite(rp_eval.scores)
                if rp_score > best_score:
                    best_chunks = rp_chunks
                    best_result = rp_result
                    best_eval   = rp_eval
                    best_score  = rp_score
                    logger.info(
                        "adaptive_retriever: rephrase re-retrieve improved score %.3f → %.3f",
                        _composite(eval_result.scores), rp_score,
                    )
                if best_eval.passed:
                    return best_chunks, best_result, best_eval
            else:
                logger.info("adaptive_retriever: rephrase unchanged — skipping re-retrieve")
        except Exception as exc:
            logger.warning("adaptive_retriever: rephrase stage failed: %s", exc)

    # ── SOURCE_GAP stages ─────────────────────────────────────────────────────
    if gap not in ("SOURCE_GAP", "BOTH"):
        return best_chunks, best_result, best_eval

    # Stage 1 — widen candidate pool
    widened_top_n = min(top_n * 2, _MAX_WIDENED_TOP_N)
    logger.info("adaptive_retriever: stage 1 — widen top_n %d → %d", top_n, widened_top_n)
    try:
        s1_chunks = retrieve(question, source_filter=source_filter, top_n=widened_top_n, top_k=top_k)
        s1_chunks = _merge_chunks(chunks, s1_chunks, top_k)
        s1_result = generate_answer(question, s1_chunks)
        s1_eval   = evaluate(question, s1_chunks, s1_result["answer"])
        s1_score  = _composite(s1_eval.scores)
        if s1_score > best_score:
            best_chunks, best_result, best_eval, best_score = s1_chunks, s1_result, s1_eval, s1_score
            logger.info("adaptive_retriever: stage 1 improved score → %.3f", s1_score)
        if best_eval.passed:
            return best_chunks, best_result, best_eval
    except Exception as exc:
        logger.warning("adaptive_retriever: stage 1 failed: %s", exc)

    # Stage 2 — query expansion + widen
    logger.info("adaptive_retriever: stage 2 — query expansion")
    try:
        expanded = _expand_query(question)
        if expanded != question:
            s2_chunks = retrieve(expanded, source_filter=source_filter, top_n=widened_top_n, top_k=top_k)
            s2_chunks = _merge_chunks(best_chunks, s2_chunks, top_k)
            s2_result = generate_answer(question, s2_chunks)
            s2_eval   = evaluate(question, s2_chunks, s2_result["answer"])
            s2_score  = _composite(s2_eval.scores)
            if s2_score > best_score:
                best_chunks, best_result, best_eval, best_score = s2_chunks, s2_result, s2_eval, s2_score
                logger.info("adaptive_retriever: stage 2 improved score → %.3f", s2_score)
            if best_eval.passed:
                return best_chunks, best_result, best_eval
        else:
            logger.info("adaptive_retriever: stage 2 skipped — expansion unchanged")
    except Exception as exc:
        logger.warning("adaptive_retriever: stage 2 failed: %s", exc)

    # Stage 3 — sub-question decomposition
    logger.info("adaptive_retriever: stage 3 — sub-question decomposition")
    try:
        sub_qs = _decompose_question(question)
        if len(sub_qs) > 1:
            merged = list(best_chunks)
            for sq in sub_qs:
                sq_chunks = retrieve(sq, source_filter=source_filter, top_n=top_n, top_k=top_k)
                merged = _merge_chunks(merged, sq_chunks, top_k)
            s3_result = generate_answer(question, merged)
            s3_eval   = evaluate(question, merged, s3_result["answer"])
            s3_score  = _composite(s3_eval.scores)
            if s3_score > best_score:
                best_chunks, best_result, best_eval, best_score = merged, s3_result, s3_eval, s3_score
                logger.info("adaptive_retriever: stage 3 improved score → %.3f", s3_score)
        else:
            logger.info("adaptive_retriever: stage 3 skipped — only one sub-question")
    except Exception as exc:
        logger.warning("adaptive_retriever: stage 3 failed: %s", exc)

    logger.info(
        "adaptive_retriever: escalation complete  final_score=%.3f  passed=%s",
        best_score,
        best_eval.passed,
    )
    return best_chunks, best_result, best_eval
