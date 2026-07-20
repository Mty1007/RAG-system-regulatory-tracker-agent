# Plan: Implement PCPD, IA, and SFC Regulator Clients

## Top-Level Overview

Implement three regulator client modules that discover PDF documents from Hong Kong financial regulators and return normalized records. The ingestion pipeline (router, schema, store) is already wired and working — only the three `discover_documents()` implementations are missing. Each client is its own PR.

**Dependencies needed:** `requests` and `beautifulsoup4` are required by tasks 2 and 3 (HTTP fetching + HTML parsing) but are absent from `requirements.txt`. They must be added before or alongside those tasks. PCPD (task 1) needs neither.

---

## Sub-Task 1 — Add HTTP/HTML dependencies to requirements.txt

**Status:** [x] done

### Intent
`requests` and `beautifulsoup4` are the standard libraries for synchronous HTTP and HTML parsing in Python. They must be in `requirements.txt` before implementing IA and SFC clients, so CI and local installs stay consistent.

### Expected Outcomes
- `requirements.txt` lists `requests>=2.32` and `beautifulsoup4>=4.12` (latest stable, actively maintained).
- `pytest-httpx` is **not** needed — the two network-dependent tests (IA) will stay live-network tests, and SFC is tested against a fixture.

### Todo List
1. Add `requests>=2.32` and `beautifulsoup4>=4.12` to `requirements.txt`.

### Relevant Context
- [`requirements.txt`](requirements.txt) — currently only has `fastapi`, `uvicorn`, `pydantic`, `pytest`, `httpx`.
- No existing HTTP client pattern to follow in `core/` — this is the first scraping work.

---

## Sub-Task 2 — Implement `PCPDClient.discover_documents()` (PR 1)

**Status:** [x] done

### Intent
PCPD is a **curated list, not a scraped source**. Return 4 hardcoded normalized records. No network calls. The full wiring (routing, schema, dedup) already exists — only the body of `discover_documents()` is missing.

### Expected Outcomes
- `PCPDClient().discover_documents()` returns exactly 4 dicts with all 7 required keys.
- `doc_id` for each is generated via `make_doc_id("PCPD", download_url)` (path-only SHA1 hash).
- `source_url` is each document's own specific PCPD page URL (not a shared listing page — PCPD has no single listing page for these 4 docs).
- `document_type` is `"Ordinance"` for PDPO, `"Guidance"` for the other 3.
- `issue_date` is `""` for all 4 (none provided in SPEC.md).
- Both skipped tests in `test/unit/test_pcpd_client.py` are un-skipped and pass.
- `POST /ingest/bulk {"source":"PCPD"}` ingests 4 docs; second call ingests 0.

### Todo List
1. In [`core/pcpd_client.py`](core/pcpd_client.py), populate `_KNOWN_DOCUMENTS` with 4 dicts, one per document from SPEC.md's PR 1 list:
   - `download_url` from SPEC.md exactly.
   - `source_url` pointing to each document's specific PCPD page (not a generic publications URL).
   - `document_type`: `"Ordinance"` for PDPO, `"Guidance"` for the remaining 3.
   - `issue_date`: `""` for all.
   - `title` from SPEC.md exactly.
   - `source`: `"PCPD"`.
   - `doc_id` via `make_doc_id("PCPD", download_url)`.
2. Implement `discover_documents()` to return a copy of `_KNOWN_DOCUMENTS`.
3. Remove the `@pytest.mark.skip` decorators from both tests in [`test/unit/test_pcpd_client.py`](test/unit/test_pcpd_client.py).
4. Run `pytest test/unit/test_pcpd_client.py` and confirm both pass.

### Relevant Context
- [`core/pcpd_client.py`](core/pcpd_client.py) — stub to fill in.
- [`core/doc_id.py`](core/doc_id.py) — `make_doc_id(source, download_url)` is the only hashing utility; use it, don't reinvent.
- [`docs/SPEC.md`](docs/SPEC.md#L77-L80) — exact 4 URLs and titles.
- [`api/routers/ingest.py`](api/routers/ingest.py) — already calls `client.discover_documents()` with no args for PCPD; no changes needed there.
- [`api/schemas.py`](api/schemas.py) — `"PCPD"` is already a valid `Source` literal.

---

## Sub-Task 3 — Implement `IAClient.discover_documents()` (PR 2)

**Status:** [x] done

### Intent
Fetch one HTML page per year from IA's listing and extract one record per `<a href="...pdf">` link on the page. The mentor confirmed: **every PDF link counts as its own document** — main circular, annexes ("Attachment:"), and Chinese translations alike. Do **not** dedup inside `discover_documents()` — let the store's doc_id dedup handle it at persist time (a handful of URLs appear twice on some pages and that's expected behaviour).

### Expected Outcomes
- `IAClient().discover_documents(2026, 2026)` returns exactly 18 docs.
- `IAClient().discover_documents(2025, 2025)` returns exactly 42 docs.
- `issue_date` is ISO `"YYYY-MM-DD"` where the row's date is a clean `"D Month YYYY"` format, `""` otherwise.
- `source_url` is the per-year listing page URL.
- `source` is `"IA"` for all records, including pre-June-2017 OCI circulars.
- `document_type` is `"Circular"` for all.
- A 404 response to a year's URL raises clearly (means "no more history"), not silently returns 0 docs.
- A 200 response with 0 PDF links raises loudly (means "parser broke").
- ~0.5–1 s polite delay between page requests.
- The two skipped tests in `test/unit/test_ia_client.py` are un-skipped and pass.

### Todo List
1. Add `requests` and `beautifulsoup4` imports to [`core/ia_client.py`](core/ia_client.py).
2. Implement `discover_documents(start_year, end_year)`:
   a. Loop `year` from `start_year` to `end_year` inclusive.
   b. Fetch the year's URL (`LISTING_URL_TMPL.format(year=year)`).
   c. On HTTP 404 → raise `ValueError(f"IA circular archive has no data for {year}: HTTP 404 (predates the archive)")`.
   d. On any other non-200 → raise `RuntimeError`.
   e. Parse HTML with BeautifulSoup: collect every `<a href="...pdf">` whose `href` ends in `.pdf`.
   f. On 200 with 0 PDF links → raise `RuntimeError(f"IA parser found 0 PDFs for {year} — page structure may have changed")`.
   g. For each matching `<a>`: title = link text (stripped); issue_date = parse the row-level date cell into ISO if it matches `D Month YYYY` pattern, else `""`; `source_url` = the year's listing page URL; `source` = `"IA"`; `document_type` = `"Circular"`.
   h. Generate `doc_id` via `make_doc_id("IA", download_url)`.
   i. Sleep `~0.75 s` between year requests (not between links on the same page).
3. Remove `@pytest.mark.skip` from both tests in [`test/unit/test_ia_client.py`](test/unit/test_ia_client.py).
4. Run `pytest test/unit/test_ia_client.py -v` against live network and confirm 18/42.

### Relevant Context
- [`core/ia_client.py`](core/ia_client.py) — stub; `BASE_URL` and `LISTING_URL_TMPL` constants already defined.
- [`core/doc_id.py`](core/doc_id.py) — `make_doc_id()`.
- [`docs/SPEC.md`](docs/SPEC.md#L87-L101) — PR 2 requirements.
- **Row structure on IA pages:** each circular occupies a `<tr>` in a plain `<table>`. A typical row has: a date cell, the main circular link, and zero or more sub-links (annexes, Chinese translation) all as `<a>` tags within that row. The acceptance numbers (18/42) prove each `<a href="*.pdf">` is its own record, regardless of its position in the row.
- **Duplicate hrefs:** 2025 page has 42 `<a href="*.pdf">` links but only 38 unique paths — return all 42 without deduplication inside the client.
- **Pre-2017 OCI circulars** keep `source: "IA"` per mentor confirmation.
- **Year floor:** 2004 → HTTP 404; 2005 → HTTP 200 with content. Split on status code, not row count.

---

## Sub-Task 4 — Update SFC fixture and implement `SFCClient` (PR 3)

**Status:** [x] done

### Intent
Implement the SFC parser with two behaviours beyond a naive `<tr>` loop: (a) only select rows with `data-code-guideline-id` (skipping all previous-versions popup `<tr>`s), and (b) for rows whose date cell is a `#popuphb-...` popup trigger (the Handbook), follow that popup and emit one record per PDF in it. Acceptance: Codes → 16, Guidelines → 51.

The existing fixture only covers the "parsing trap" (2 real rows + 1 previous-versions popup). It must be extended to also cover the Handbook popup path, since the mentor explicitly requires the regression test prove: direct rows parsed, latest-version popup PDFs included, previous-versions junk excluded.

### Expected Outcomes
- `SFCClient.parse_listing_page(html, base_url, "Code")` on the updated fixture returns the correct count: 2 direct rows + 5 Handbook PDFs = 7 docs (fixture-specific number, not 16; the fixture is trimmed).
- `test_direct_pdf_rows_parsed` passes: fixture yields exactly 2 docs from direct-PDF rows.
- `test_handbook_popup_pdfs_included` passes: 5 Handbook PDFs are present in the result.
- `test_previous_versions_excluded` passes: no date-range strings appear as titles.
- `discover_documents()` on live network: Codes → 16, Guidelines → 51.
- Zero-results guard raises `RuntimeError` if either page yields 0 attribute rows.
- `doc_id` hashes URL path only (strips `?rev=...`) via `make_doc_id()`.
- `source_url` is the listing page URL (Codes page or Guidelines page, not a shared constant).
- `title` comes from the first `<td>` of the attribute row (not the PDF `<a>` link text which is just a date).
- For Handbook popup PDFs: title from the `<h4>` heading above each PDF's table (or the Handbook's own row title for the "Latest version" entry itself); `issue_date` from the PDF link text.
- Both tests in `test/unit/test_sfc_client.py` are un-skipped and pass.
- `docs/SPEC.md` PR 3 acceptance updated from `Codes → 11` to `Codes → 16` with a note on the Handbook popup rule.

### Todo List
1. **Extend `test/unit/fixtures/sfc_codes_sample.html`:** Add the Handbook row (with its `<a class="popup-btn" data-popup-id="#popuphb-<id>">` date cell) and its `#popuphb-<id>` popup block containing 5 PDFs (full Handbook + Sections I–IV, each under its own `<h4>`). Keep the existing 2 direct rows and the existing previous-versions popup intact. Note: `docs/SPEC.md` already has `Codes → 16` (fixed by mentor in commit `597f4cd`) — do not re-edit SPEC.md.
2. **Replace `test/unit/test_sfc_client.py`** with three focused tests (delete the existing `test_popup_rows_are_excluded`):
   a. `test_direct_pdf_rows_parsed`: call `parse_listing_page` on the fixture; assert exactly 2 of the returned docs have titles matching the two known direct-PDF rows.
   b. `test_handbook_popup_pdfs_included`: assert exactly 5 docs with Handbook/Section titles are present in the result (from the `#popuphb-...` popup block).
   c. `test_previous_versions_excluded`: assert no returned doc has a title containing a date-range string (e.g. `"16 Nov 2022 - 1 Jan 2026"`).
   d. No `@pytest.mark.skip` decorators on any test.
3. **Implement `SFCClient.parse_listing_page(html, base_url, document_type)`:**
   a. Use `soup.select('tr[data-code-guideline-id]')` to get only real rows.
   b. If result is empty → raise `RuntimeError("SFC parser found 0 real rows — page structure may have changed")`.
   c. For each row: extract `title` from `row.find('td').get_text(strip=True)`.
   d. Inspect the date cell (2nd `<td>`): if it contains an `<a>` with `href` ending in `.pdf` → one record, `download_url` from that href, `issue_date` from the link text (parsed to ISO if it is a valid date, else `""`).
   e. If the date cell contains `<a class="popup-btn" data-popup-id>` whose `data-popup-id` starts with `#popuphb-` → locate that popup block in the soup by its `id`; for each PDF `<a>` in the popup, emit one record: `title` from the nearest preceding `<h4>` (or the row's own `<td>` title for the top-level "Latest version" entry), `download_url` from the `<a href>`, `issue_date` from the link text.
   f. For all records: `doc_id = make_doc_id("SFC", download_url)` — source is always `"SFC"` regardless of document type label; `source_url` passed in from caller; `document_type` passed in parameter.
4. **Implement `SFCClient.discover_documents()`:**
   a. Import `requests` and `time`.
   b. For each `(document_type, url)` in `LISTING_URLS.items()`:
      - Fetch the page with `requests.get(url, timeout=self.timeout_sec)`, raise on non-200.
      - Call `parse_listing_page(response.text, BASE_URL, document_type)`.
      - Accumulate records.
      - Sleep ~0.75 s between the two page fetches.
   c. Return combined list.
5. Run `pytest test/unit/test_sfc_client.py -v` and confirm all tests pass against the fixture.

### Relevant Context
- [`core/sfc_client.py`](core/sfc_client.py) — stub; `BASE_URL` and `LISTING_URLS` constants already defined; `parse_listing_page` static method signature already declared.
- [`test/unit/fixtures/sfc_codes_sample.html`](test/unit/fixtures/sfc_codes_sample.html) — current fixture: 2 real rows + 1 previous-versions popup. Needs the Handbook row + `#popuphb-...` block added.
- [`test/unit/test_sfc_client.py`](test/unit/test_sfc_client.py) — current test checks `len == 2`; must update to reflect Handbook PDFs.
- [`docs/SPEC.md`](docs/SPEC.md#L133) — already updated to `Codes → 16` by mentor in commit `597f4cd`. No edits needed.
- **Key selector:** `soup.select('tr[data-code-guideline-id]')` — this is the entire discrimination logic. No additional filtering needed.
- **Popup ID pattern:** `data-popup-id="#popuphb-<id>"` for Handbook latest-version popup; `data-popup-id="#popup<id>"` (no `hb`) for previous-versions popup. Only follow `#popuphb-...`.
- **`?rev=` stripping:** handled automatically by `make_doc_id()` since it hashes `urlparse(url).path` which excludes query strings.
- [`core/doc_id.py`](core/doc_id.py) — `make_doc_id(source, download_url)` where source is always `"SFC"`.

---

## Implementation Order

```
Sub-Task 1 (add deps) → Sub-Task 2 (PCPD) → Sub-Task 3 (IA) → Sub-Task 4 (SFC)
```

Sub-task 1 is a prerequisite for 3 and 4. Sub-task 2 is independent and can be done any time after sub-task 1. Sub-tasks 3 and 4 each need sub-task 1. Sub-tasks 3 and 4 are independent of each other.
