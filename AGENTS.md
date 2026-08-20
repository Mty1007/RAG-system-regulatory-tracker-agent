# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Project

Python 3.11 FastAPI RAG agent for Hong Kong regulatory documents (SFC and PCPD). No `pyproject.toml` or `setup.py` — plain `requirements.txt`.

IBM watsonx Orchestrate integration: import `docs/orchestrate_openapi.yaml` as an HTTP Tool in Orchestrate. Three callable operations: `ask_regulatory_question`, `discover_documents`, `bulk_ingest_documents`. Auth: `Authorization: Bearer <ORCHESTRATE_API_KEY>`.

## Commands

```bash
# Install
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
git config core.hooksPath .githooks   # activates commit-msg hook (required)

# Run API
uvicorn api.main:app --reload --port 8000

# All unit tests
python -m pytest test/unit/ -v

# Single test
python -m pytest test/unit/test_chunker.py::test_98_percent_stays_in_single_chunk -v

# Lint (CI configuration)
pip install ruff
ruff check api/ core/ store/ --select=E,F,I --ignore=F401,E501

# Syntax check (mirrors CI)
python -m compileall api/ core/ store/ -q

# Offline RAG quality eval (requires a running API and rag_eval_log.csv)
python scripts/eval_quality.py
```

## Architecture

```
POST /chat → retrieve() → fetch_fallback_passages() → rerank()
          → generate_answer() → evaluate() → [escalate()] → log_request()
POST /ingest/discover → SFCClient/PCPDClient.discover_documents()
POST /ingest/bulk     → DocumentStore (in-memory) or COSDocumentStore
POST /eval/online     → evaluate() → [escalate()] (Orchestrate quality tool)
```

**Pipeline flow (offline scripts):**
```
SFC/PCPD websites → scripts/run_ocr.py → COS transformed/<doc_id>.md
                  → scripts/run_chunk.py → AstraDB chunks collection + COS chunks/<doc_id>.jsonl
```

## Critical Patterns

**`doc_id` generation** — always use `core/doc_id.make_doc_id(source, download_url)`. Hashes the URL **path only**, never the full URL. SFC URLs carry a `?rev=` cache-busting query string; hashing the full URL would create duplicate doc_ids on republish.

**AstraDB embeddings** — chunks are written with `$vectorize` and `$lexical` fields; AstraDB auto-embeds via NVIDIA `nvidia/nv-embedqa-e5-v5`. Do **not** call `core/embedder.embed_texts()` for chunk storage — that's used only for the legacy OCR pipeline. Retrieval uses `find_and_rerank()` with `$hybrid` sort.

**Chunker limits** — `DEFAULT_MAX_CHARS=480`, `DEFAULT_MIN_CHARS=80`. Character-based (not token-based) because CJK text has no whitespace; 480 chars ≈ 320 tokens worst-case for dense Chinese text. Strip Docling `<!-- ... -->` HTML comments and `\n---\n` page-break dividers **before** chunking — they corrupt sentence continuity.

**Reranker is a pass-through** — `RERANKER=astradb` (default) means `core/reranker.rerank()` is a no-op; reranking happens inside `core/retriever.retrieve()` via AstraDB's built-in NVIDIA reranker. Set `RERANKER=watsonx` or `RERANKER=local` for the other backends.

**Cross-lingual retrieval** — queries with ≥3 CJK characters trigger a second AstraDB search using a WatsonX-translated English query; results are merged and deduplicated by `_id`.

**`_KEYWORD_DOC_MAP`** in `core/retriever` — hardcoded pattern→doc_id map that force-injects chunks from specific docs when queries match known vocabulary-mismatch keywords (e.g., "internal audit" → `sfc-a90505b192cd`). Respect `source_filter` — don't inject SFC docs when caller restricted to PCPD.

**Cross-reference chunk filter** — `core/generator._filter_crossref_chunks()` removes short (<300 chars) chunks that are ≥80% "please refer to…" sentences. Falls back to original list if <3 chunks would remain.

**IAM token** — `core/embedder._get_iam_token()` caches the IBM Cloud IAM token for 50 min (valid 60 min, 10 min safety buffer). Reused across `core/generator`, `core/retriever`, and `core/reranker` via import.

**`USE_COS` env var** — if set, `api/dependencies.init_services()` uses `COSDocumentStore` instead of the in-memory `DocumentStore`. The lazy import of `COSDocumentStore` keeps COS credentials optional. Also gates `core/cos_retriever.fetch_fallback_passages()` — COS fallback is silently skipped without it.

**`ibm-watsonx-gov` key naming** — `scripts/eval_quality.py` AND `core/online_eval.py` both bridge `WATSONX_API_KEY` → `WATSONX_APIKEY` (no underscore, required by the gov SDK). Replicate this bridge in any new eval script.

**`source` field** — derived from `doc_id` prefix: `sfc-xxx` → `"SFC"`, `pcpd-xxx` → `"PCPD"`. The `docs/<doc_id>.json` metadata pattern is **not used** — do not read or write it.

**Online evaluator (block-and-replace)** — `core/online_eval.evaluate()` runs synchronously in `POST /chat/` after `generate_answer()`. If score < `ONLINE_EVAL_THRESHOLD` (default 0.55), `core/adaptive_retriever.escalate()` is called before the HTTP response is returned. The user always receives the improved answer. Escalation failures are caught and the original answer is returned — never suppressed.

**Adaptive retriever stages** — SOURCE_GAP triggers 3 stages in order: (1) widen `top_n` ×2 capped at 80, (2) query expansion via `_expand_query()`, (3) sub-question decomposition. Each stage re-scores; stops at the first passing stage. ANSWER_REPHRASE_GAP skips retrieval entirely — re-generates with `_REPHRASE_DIRECTIVE` prepended to system prompt via a temporary monkey-patch of `core.generator._SYSTEM_PROMPT`.

**COS full-doc fallback** — `core/cos_retriever.fetch_fallback_passages()` fires when `max(rerank_score) < COS_FALLBACK_THRESHOLD` (default 0.35) AND `USE_COS` is set. Downloads `transformed/<doc_id>.md` from COS for top-3 docs, extracts ±2 sections around matched heading, caps at 4000 chars. Returned passages are appended **after** reranked chunks (never compete for top slots). `source_type="cos_fallback"` distinguishes them.

**`MAX_CONTEXT_COLS=20`** in `core/rag_eval` — raised from 10 to cover the new `top_k` default of 15 plus COS fallback passages. The offline evaluator (`scripts/eval_quality.py`) derives context columns dynamically from CSV headers so it handles both old (10-col) and new (20-col) logs without changes.

**Eval log new columns** — `gap_type`, `pre_score`, `post_score` appended to `rag_eval_log.csv`. Empty for clean (non-escalated) requests. Use these to analyse where escalation fires and whether it improved scores.

**Orchestrate inbound auth** — `ORCHESTRATE_API_KEY` env var enables `Authorization: Bearer <key>` checking on all inbound requests via an HTTP middleware in `api/main.py`. Leave unset for local dev. `ORCHESTRATE_ALLOWED_ORIGINS` controls CORS (comma-separated; defaults to localhost only).

## Code Style

- Every module starts with `from __future__ import annotations`
- All logging via `logger = logging.getLogger(__name__)` — never `print()` in library code
- Type hints on all public functions; private helpers may omit
- Inline comments use `# ── section name ───` (em-dash ruled lines) for visual sectioning
- HTTP calls: use `requests` directly (not `httpx`); always set explicit `timeout=`
- Pydantic v2 schemas live in `api/schemas.py` (shared) or inline in router files for route-local models
- Tests: no fixtures directory magic — test functions import directly from source modules; the only fixture is `test/unit/fixtures/sfc_codes_sample.html` for HTML scraping tests

## Commit Convention (enforced by CI and `.githooks/commit-msg`)

```
type(scope): description
```
Types: `feat fix refactor perf style test docs build ops chore`

PRs with `fix` commits **must** include changes to `test/` files (CI regression-test-check enforces this).
