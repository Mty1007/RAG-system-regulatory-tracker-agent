# Delivery Summary — Regulator Clients (PR 1 · PR 2 · PR 3)

Completed: 2026-07-19  
All 7 unit tests pass · All live-network acceptance counts confirmed · Idempotency verified

---

## What was built

Three `discover_documents()` implementations that fetch PDF metadata from Hong Kong
financial regulators and return normalized records ready for the ingestion pipeline.
The pipeline itself (router, schema, dedup store) was already wired — only the three
client bodies were missing.

Each record has the same shape:

```python
{
    "doc_id":        str,   # "source-<sha1[:12] of URL path>"
    "source":        str,   # "PCPD" | "IA" | "SFC"
    "title":         str,
    "download_url":  str,   # direct .pdf link
    "source_url":    str,   # listing page the doc was found on
    "document_type": str,   # "Ordinance" | "Guidance" | "Circular" | "Codes" | "Guidelines"
    "issue_date":    str,   # ISO "YYYY-MM-DD" if parseable, else ""
}
```

---

## PR 1 — PCPD (`core/pcpd_client.py`)

**Approach:** curated static list — no scraping.  
PCPD has no single relevant listing page, so the 4 known compliance documents
are hardcoded and returned directly. `doc_id` is still computed via `make_doc_id()`
so dedup works identically to scraped sources.

| # | Title | Type |
|---|---|---|
| 1 | PDPO Full Ordinance | Ordinance |
| 2 | Six Data Protection Principles — Overview | Guidance |
| 3 | Data Security Measures Guidance | Guidance |
| 4 | Data Breach Handling Guidance Note | Guidance |

**Tests:** `test/unit/test_pcpd_client.py` — 2 / 2 pass  
**Acceptance:** 4 docs ingested on first call · 0 on second call (idempotent) ✓

---

## PR 2 — IA (`core/ia_client.py`)

**Approach:** one HTTP fetch per year, extract every `<a href="*.pdf">` as its own record.

Key decisions:
- Every PDF link is its own document — main circular, annexes, and Chinese translations alike. Dedup happens at store level via `doc_id`, not inside the client.
- `issue_date` is parsed from the row's first `<td>` (date column) into ISO `YYYY-MM-DD` when it matches `D Month YYYY` format; `""` otherwise.
- HTTP 404 raises `ValueError` ("predates the archive") so callers can detect the year-floor cleanly.
- HTTP 200 with 0 PDF links raises `RuntimeError` ("parser broke") — never silently returns empty.
- 0.75 s polite delay between year requests.

**Live acceptance (confirmed against ia.org.hk):**

| Year | Documents |
|---|---|
| 2026 | **18** ✓ |
| 2025 | **42** ✓ |
| 2004 | raises `ValueError` (predates archive) ✓ |

**Tests:** `test/unit/test_ia_client.py` — 2 / 2 pass (live network)

---

## PR 3 — SFC (`core/sfc_client.py`)

**Approach:** two HTML pages (Codes + Guidelines), with two non-obvious parsing behaviours.

### Parsing trap avoided
The SFC pages contain two kinds of `<tr>`:
- **Real document rows** — carry `data-code-guideline-id="..."` attribute.
- **Previous-versions popup rows** — no attribute; each `<td>` is just a date range like `"16 Nov 2022 - 1 Jan 2026"`.

Selector used: `soup.select('tr[data-code-guideline-id]')` — this is the entire
discrimination logic. A naive "every `<tr>` with a `.pdf` link" would return 99
Codes rows and 108 Guidelines rows; the correct counts are 12 and 51.

### Handbook popup expansion
One Codes row (the SFC Handbook) has no direct PDF — its date cell is a popup
trigger (`data-popup-id="#popuphb-..."`) pointing at a "Latest version" block
containing **5 PDFs**: the full Handbook plus Sections I–IV. Each becomes its own
record. Only `#popuphb-...` popups are followed; `#popup...` (no `hb`) are the
previous-versions popups and are excluded.

**Title rule:** comes from the row's first `<td>`, not from the PDF link text
(which is just a date like `"2 Jan 2026"`). For Handbook popup PDFs, title comes
from the preceding `<h4>` heading, or the row title itself for the first PDF.

**doc_id hashing:** URL path only — `?rev=<hash>` cache-busting query strings are
stripped automatically by `make_doc_id()`, keeping doc identity stable across
republishes.

**Live acceptance (confirmed against sfc.hk):**

| Page | Documents |
|---|---|
| Codes | **16** (11 direct-PDF rows + 5 Handbook popup PDFs) ✓ |
| Guidelines | **51** ✓ |
| Total | **67** ✓ |

**Tests:** `test/unit/test_sfc_client.py` — 3 / 3 pass (fixture, no network)

| Test | Proves |
|---|---|
| `test_direct_pdf_rows_parsed` | 2 direct-PDF rows are returned |
| `test_handbook_popup_pdfs_included` | all 5 Handbook popup PDFs are returned |
| `test_previous_versions_excluded` | no date-range junk leaks into results |

---

## Files changed

| File | Change |
|---|---|
| `requirements.txt` | Added `requests>=2.32`, `beautifulsoup4>=4.12` |
| `core/pcpd_client.py` | Implemented `_KNOWN_DOCUMENTS` + `discover_documents()` |
| `core/ia_client.py` | Implemented `discover_documents(start_year, end_year)` |
| `core/sfc_client.py` | Implemented `parse_listing_page()` + `discover_documents()` |
| `test/unit/test_pcpd_client.py` | Removed `@pytest.mark.skip` from both tests |
| `test/unit/test_ia_client.py` | Removed `@pytest.mark.skip` from both tests |
| `test/unit/test_sfc_client.py` | Replaced old skipped test with 3 focused tests |
| `test/unit/fixtures/sfc_codes_sample.html` | Extended with Handbook row + `#popuphb-...` popup block (5 PDFs) |
| `demo.py` | Self-contained demo script — run `.venv/bin/python demo.py` |

---

## Run the demo

```bash
.venv/bin/python demo.py
```

Runs all acceptance checks end-to-end (live network for IA and SFC) and prints
a `✓` or `✗` for each assertion. Exit code 0 = all pass.

## Run the unit tests

```bash
.venv/bin/python -m pytest test/unit/ -v
```

Expected: **7 passed**.
