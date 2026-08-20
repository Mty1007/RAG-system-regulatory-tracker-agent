"""Offline RAG quality evaluation using ibm-watsonx-gov v1.5.

Run after the API has received at least one request:

    python scripts/eval_quality.py

The script reads rag_eval_log.csv (written by core/rag_eval.py on every
/chat request) and scores each row for:
  - RETRIEVAL_QUALITY  (context relevance — were the right chunks fetched?)
  - ANSWER_QUALITY     (faithfulness + answer relevance — is the answer grounded?)
"""

import os
import pathlib
import sys

# Load .env so WATSONX_API_KEY is available, then alias it to the name
# ibm-watsonx-gov v1.5 expects (WATSONX_APIKEY — no underscore before KEY).
_env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ibm-watsonx-gov reads WATSONX_APIKEY; our .env uses WATSONX_API_KEY — bridge them.
if "WATSONX_APIKEY" not in os.environ and "WATSONX_API_KEY" in os.environ:
    os.environ["WATSONX_APIKEY"] = os.environ["WATSONX_API_KEY"]

import pandas as pd
from ibm_watsonx_gov.config import GenAIConfiguration
from ibm_watsonx_gov.evaluators import MetricsEvaluator
from ibm_watsonx_gov.metrics.context_relevance.context_relevance_metric import ContextRelevanceMetric
from ibm_watsonx_gov.metrics.faithfulness.faithfulness_metric import FaithfulnessMetric
from ibm_watsonx_gov.metrics.answer_relevance.answer_relevance_metric import AnswerRelevanceMetric

log_path = pathlib.Path(os.environ.get("RAG_EVAL_LOG", "rag_eval_log.csv"))
if not log_path.exists():
    print(f"ERROR: {log_path} not found — send some API requests first.")
    sys.exit(1)

df = pd.read_csv(log_path, dtype={"record_id": str})
# Ensure no NaN record_ids — fill any missing ones
import uuid as _uuid
df["record_id"] = df["record_id"].apply(
    lambda v: v if isinstance(v, str) and v.strip() else str(_uuid.uuid4())
)
print(f"Loaded {len(df)} rows from {log_path}\n")

if len(df) == 0:
    print("ERROR: log file is empty — send some questions to the API first.")
    sys.exit(1)

# Derive context_fields from whichever contextN columns are present in the CSV
# (the number varies depending on how many chunks the LLM actually received).
_context_cols = [c for c in df.columns if c.startswith("context")]
if not _context_cols:
    print("ERROR: no context columns found in log — re-generate the log with the updated core/rag_eval.py.")
    sys.exit(1)

cfg = GenAIConfiguration(
    input_fields=["input_text"],
    context_fields=_context_cols,
    output_fields=["generated_text"],
)

# sentence_bert_mini_lm: semantic similarity — far more accurate than token
# overlap for regulatory text where the LLM paraphrases formal legalese.
metrics = [
    ContextRelevanceMetric(method="sentence_bert_bge"),   # retrieval-tuned model
    FaithfulnessMetric(method="sentence_bert_mini_lm"),
    AnswerRelevanceMetric(),
]

evaluator = MetricsEvaluator(configuration=cfg)
results   = evaluator.evaluate(df, metrics=metrics)

# ── Answer Similarity — computed manually on the golden rows ─────────────────
# The SDK's AnswerSimilarityMetric cannot be called in the same process after
# another evaluate() call due to an event-loop conflict in nest_asyncio.
# We replicate token_recall directly: |tokens(answer) ∩ tokens(truth)| / |tokens(truth)|
def _token_recall(pred: str, ref: str) -> float:
    pred_tok = set(str(pred).lower().split())
    ref_tok  = set(str(ref).lower().split())
    if not ref_tok:
        return 0.0
    return len(pred_tok & ref_tok) / len(ref_tok)

df_gt = df[df["ground_truth"].notna() & (df["ground_truth"].str.strip() != "")].copy()
if not df_gt.empty:
    df_gt["answer_similarity"] = df_gt.apply(
        lambda r: _token_recall(r["generated_text"], r["ground_truth"]), axis=1
    )
    sim_mean  = df_gt["answer_similarity"].mean()
    sim_pass  = "✓ PASS" if sim_mean >= 0.70 else "✗ FAIL"
    sim_rows  = len(df_gt)
else:
    sim_mean, sim_pass, sim_rows = 0.0, "N/A", 0

# Print per-metric aggregate summary
print(f"{'Metric':<22} {'Score':>7}  {'Pass?':>6}  {'Rows':>5}  Method")
print("-" * 65)
for m in results.metrics_result:
    passed = "✓ PASS" if (m.value or 0) >= (m.thresholds[0].value if m.thresholds else 0) else "✗ FAIL"
    rows   = m.total_records if hasattr(m, "total_records") else "?"
    print(f"{m.display_name:<22} {m.value or 0:>7.3f}  {passed:>6}  {rows:>5}  {m.method}")
print(f"{'Answer Similarity':<22} {sim_mean:>7.3f}  {sim_pass:>6}  {sim_rows:>5}  token_recall (golden rows only)")

# Write full per-row detail to CSV
out_path = pathlib.Path("rag_eval_scores.csv")
df_out = results.to_df()
if not df_out.empty:
    df_out.to_csv(out_path, index=False)
    print(f"\nPer-row scores written to {out_path}")
