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

## ✅ Resolved — IA HTTP 403

`core/ia_client.py` uses `patchright` (headed Chrome) to bypass Cloudflare.
Persistent profile at `~/.ia-chrome-profile` stores the clearance cookie.
Do not run `IAClient.discover_documents()` in CI — headed browser only.

---

## ✅ Resolved — watsonx Docling OCR

`run_ocr.py` uses `TextExtractionsV2` with `WATSONX_COS_CONNECTION_ASSET_ID`.
All 71 PDFs processed. No bbox data exposed by this API — layout store stays
empty but the code is ready for local Docling if needed later.

---

## 🔧 Next Step — populate $lexical (FORCE_RECHUNK)

All 71 docs were chunked **before** the `$lexical` fix was in place, so BM25
search returns 0 hits. Run this once to re-write every chunk with `$lexical`:

```bash
cd /Users/matsunyan/regulatory-tracker-agent
set -a && source .env && set +a
FORCE_RECHUNK=1 .venv/bin/python scripts/run_chunk.py 2>&1 | grep -E "CHUNK|OK |FAIL|TOTAL"
```

Expected: `TOTAL  processed=71  skipped=0  failed=0`

Then verify hybrid search is live:

```bash
.venv/bin/python3 - << 'EOF'
import os
from astrapy import DataAPIClient
token    = os.environ["ASTRA_DB_APPLICATION_TOKEN"]
endpoint = os.environ["ASTRA_DB_API_ENDPOINT"].rstrip("/")
keyspace = os.environ.get("ASTRA_DB_KEYSPACE", "default_keyspace")
col = DataAPIClient(token).get_database(endpoint, keyspace=keyspace).get_collection("chunks")

print("=== BM25 English: 'client asset segregation' ===")
hits = list(col.find({}, sort={"$lexical": "client asset segregation"}, limit=3, projection={"text": 1, "_id": 1}))
for h in hits: print(f"  {h['_id'][:35]}  {h['text'][:80]!r}")
print(f"  -> {len(hits)} hits")

print("\n=== BM25 Chinese: '個人資料' ===")
hits = list(col.find({}, sort={"$lexical": "個人資料"}, limit=3, projection={"text": 1, "_id": 1}))
for h in hits: print(f"  {h['_id'][:35]}  {h['text'][:80]!r}")
print(f"  -> {len(hits)} hits")
EOF
```

---

## 🔧 Next Step — Test chat API

Start the server:

```bash
cd /Users/matsunyan/regulatory-tracker-agent
set -a && source .env && set +a
.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

Then query it:

```bash
curl -X POST http://127.0.0.1:8000/chat/ \
  -H "Content-Type: application/json" \
  -d '{"question": "What are SFC requirements for client asset segregation?", "source_filter": "SFC"}'
```

Expected response shape:

```json
{
  "answer": "...",
  "citations": [{"doc_id": "sfc-...", "source": "SFC", "section_heading": "...", "page_start": 0}],
  "model_used": "ibm/granite-13b-chat-v2",
  "chunk_count": 5
}
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
| `store/astra_chunk_store.py` | AstraDB chunks collection ($vector + $lexical) |
| `store/astra_layout_store.py` | AstraDB layout_elements table (bbox — empty until local Docling) |
| `core/retriever.py` | Hybrid ANN + BM25 ($lexical) search with RRF merge |
| `core/chunker.py` | Markdown-aware heading-split + sliding-window chunker |
| `core/embedder.py` | WatsonX granite-embedding-278m-multilingual (768-dim) |
| `core/reranker.py` | Cross-encoder reranker (local or WatsonX) |
| `core/generator.py` | IBM Granite answer generation |
| `api/routers/chat.py` | POST /chat/ — retrieve → rerank → generate |
| `.env.example` | All required environment variables |
