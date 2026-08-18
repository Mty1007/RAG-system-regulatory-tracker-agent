# Regulatory Tracker Agent — Handoff Notes

## Current Branch
`fix/regulator-client-bugs`

---

## Pipeline Architecture

```
Source website (SFC / PCPD)
    │  discover_documents()
    ▼
Download PDF ──────────────────────► COS  pdfs/<doc_id>.pdf
    │
    ▼
TextExtractionsV2 (WatsonX Docling)
    │  Markdown text only
    │  (TextExtractionsV2 does not expose bbox — layout store stays empty)
    ▼
COS  transformed/<doc_id>.md        ← canonical readable output

    ▼
chunk_markdown()  (heading-split + sliding-window, 480-char, EN+ZH safe)
    │
    ▼
embed_texts()  (ibm/granite-embedding-278m-multilingual, 768-dim)
    │
    ├──► AstraDB  collection: chunks
    │        $vector   768-dim COSINE    ← semantic ANN search
    │        $lexical  text              ← BM25 keyword search (EN + CJK)
    │        doc_id, source, chunk_index,
    │        section_heading, page_start,
    │        text, token_count
    │
    └──► COS  chunks/<doc_id>.jsonl     ← text backup, no vectors

    ▼
Retriever  — semantic ($vector) + keyword ($lexical) → RRF merge
    ▼
Reranker   — cross-encoder (local) or WatsonX Rerank API
    ▼
Generator  — IBM Granite LLM  (ibm/granite-13b-chat-v2)
    ▼
POST /chat/  →  { answer, citations, model_used, chunk_count }
```

**COS holds only three prefixes:**
| Prefix | Contents |
|--------|----------|
| `pdfs/` | Original source PDFs (authoritative, never modified) |
| `transformed/` | Docling Markdown output per doc |
| `chunks/` | JSONL text backup per doc (no vectors) |

`docs/<doc_id>.json` records are **not written or read**. Source is derived
from the `doc_id` prefix (`sfc-xxx` → `"SFC"`, `pcpd-xxx` → `"PCPD"`).

---

## ✅ Completed

| Item | Detail |
|------|--------|
| AstraDB `chunks` collection | 768-dim COSINE + lexical (BM25) enabled |
| 71 PDFs in COS | 4 PCPD + 67 SFC under `pdfs/` |
| OCR pipeline | `run_ocr.py` — all 71 docs → `transformed/*.md` |
| Chunk + embed pipeline | `run_chunk.py` — all 71 docs in AstraDB + COS JSONL |
| `$lexical` field | Fixed — chunks now store `$lexical=text` for BM25 |
| `$text` → `$lexical` retriever | Fixed — keyword search sort key corrected |
| Idempotency fix | Skip only when JSONL count == AstraDB count (not just JSONL existence) |
| Delete before upsert | `delete_chunks()` before `upsert_chunks()` prevents stale accumulation |
| Embed timeout | Raised 60 s → 120 s (large docs were timing out) |
| Source from doc_id prefix | Removed `docs/<doc_id>.json` COS read from chunk pipeline |
| Unit tests | 13/13 passing |

---

## ✅ Resolved — AstraDB mismatch (3 docs)

After the first chunking run, 3 docs had more chunks in AstraDB than in the
JSONL backup (mid-upsert disconnect / stale data from earlier runs):

| Doc | JSONL | Old AstraDB | Fixed |
|-----|-------|-------------|-------|
| `sfc-c23b1a6363ca` | 10 | 16 | ✅ |
| `sfc-df4783165ec8` | 20 | 24 | ✅ |
| `sfc-eeffc9e341b1` | 48 | 89 | ✅ |

Deleted stale AstraDB chunks + JSONL backups, re-ran pipeline. All 71 now
report `SKIP (N chunks, AstraDB in sync)`.

---

## ✅ Resolved — watsonx Docling OCR

`run_ocr.py` uses `TextExtractionsV2` with `WATSONX_COS_CONNECTION_ASSET_ID`.
All 71 PDFs processed. No bbox data exposed by this API — layout store stays
empty but the code is ready for local Docling if needed later.

---

## ✅ System Status

- Chat API running on port 8001, answering SFC + PCPD questions correctly
- Hybrid retrieval working (ANN vector confirmed; BM25 English leg optional improvement via `FORCE_RECHUNK=1`)
- RAG quality evaluation pipeline in place — run `python scripts/eval_quality.py` any time

## Start the server

```bash
cd /Users/matsunyan/regulatory-tracker-agent
set -a && source .env && set +a
.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8001 --reload
```

## Run quality evaluation

```bash
.venv/bin/python scripts/eval_quality.py
```

## Seed more eval questions

```bash
.venv/bin/python scripts/seed_eval_log.py
```

---

## Environment Setup (every new terminal)

```bash
cd /Users/matsunyan/regulatory-tracker-agent
set -a && source .env && set +a
```

## Key Files

| File | Purpose |
|------|---------|
| `scripts/run_ocr.py` | OCR pipeline — TextExtractionsV2 (WatsonX Docling) |
| `scripts/run_chunk.py` | Chunk + embed pipeline |
| `scripts/eval_quality.py` | RAG quality scorer (context relevance, faithfulness, answer relevance, answer similarity) |
| `scripts/seed_eval_log.py` | Send batch questions to API to populate eval log |
| `scripts/add_ground_truths.py` | Add expert reference answers to eval log for answer similarity scoring |
| `core/rag_eval.py` | Auto-logs every /chat request to rag_eval_log.csv |
| `store/astra_chunk_store.py` | AstraDB chunks collection ($vector + $lexical) |
| `core/retriever.py` | Hybrid ANN + BM25 search via AstraDB find_and_rerank() |
| `core/chunker.py` | Markdown-aware heading-split + sliding-window chunker |
| `core/reranker.py` | Reranker — AstraDB NVIDIA (default), WatsonX, or local |
| `core/generator.py` | Mistral via WatsonX answer generation |
| `api/routers/chat.py` | POST /chat/ — retrieve → rerank → generate → log |
| `.env.example` | All required environment variables |
