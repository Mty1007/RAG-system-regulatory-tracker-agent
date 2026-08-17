# Spec: Regulatory Tracker Agent — Ingestion

## Goal

Discover PDFs from 2 Hong Kong financial regulator sources (SFC and PCPD)
and persist a normalized document record (metadata + `download_url`) for
each one. No search API exists for either source — each is a static HTML
listing page (or, for PCPD, a fixed small set of known documents) that must
be scraped.

Actually downloading and storing the PDF bytes is out of scope for this
project — that step depends on a real object-storage backend and belongs in
whatever system eventually receives this pipeline's output. Stop at
"discover + persist metadata."

## Normalized record shape

Every source's `discover_documents()` must return a list of dicts shaped like:

```python
{
    "doc_id": str,          # see doc_id rule below
    "source": str,          # "SFC" | "PCPD"
    "title": str,
    "download_url": str,    # direct PDF link
    "source_url": str,      # the listing page it came from
    "document_type": str,   # e.g. "Code", "Guideline", "Circular", "Guidance"
    "issue_date": str,      # ISO "YYYY-MM-DD" if known, else ""
}
```

## Hard rule: doc_id hashing

```python
doc_id = f"{source.lower()}-{sha1(urlparse(download_url).path.encode()).hexdigest()[:12]}"
```

**Hash the URL path only — never the full URL.** SFC's PDF links carry a
`?rev=<hash>` cache-busting query string that changes when a document is
republished. If the full URL (including the query string) were hashed, every
republish would mint a new `doc_id`, and the "already ingested, skip"
dedup check would miss it — the same document would silently re-ingest as a
brand-new record forever. Hashing the path only keeps the identity stable
across republishes.

## Why these sources aren't all scraped the same way

HKMA (our existing pipeline, not part of this project) can "scan everything"
because it has a real paginated search API. Neither of these 2 sources have
that, so each is handled on its own terms:

- **SFC already scans everything, no year loop needed.** The Codes and
  Guidelines pages aren't paginated — each page already lists literally
  every *current* document in one shot. There's nothing more to discover
  by adding a year parameter; the full page IS the full corpus.
- **PCPD stays a curated list, deliberately.** PCPD's site does have a
  larger structured feed of publications, but it spans everything from
  children's privacy to HR to direct marketing — mostly irrelevant to this
  project, and pulling it in wholesale would dilute retrieval quality with
  noise. The 4 known documents below are what's actually relevant; this is
  a conscious choice, not something left half-finished. If PCPD publishes
  new relevant guidance later, add it to the list by hand.

## PR 1 — PCPD (foundation)

- `core/pcpd_client.py`: no scraping needed, this is a fixed set of 4 known
  documents. Return them directly as normalized records:
  - `https://www.pcpd.org.hk/english/files/pdpo.pdf` — "PDPO Full Ordinance"
  - `https://www.pcpd.org.hk/english/education_training/individuals/public_seminars/files/PDPO_eng_2025.pdf` — "Six Data Protection Principles — Overview"
  - `https://www.pcpd.org.hk/english/resources_centre/publications/files/guidance_datasecurity_e.pdf` — "Data Security Measures Guidance"
  - `https://www.pcpd.org.hk/english/resources_centre/publications/files/guidance_note_dbn_e.pdf` — "Data Breach Handling Guidance Note"
- Wire `source` through `api/schemas.py` → `api/services/ingestion.py` →
  `api/routers/ingest.py` → `api/dependencies.py`.
- **Acceptance:** `/ingest/bulk?source=PCPD` ingests exactly 4 docs. Running
  it a second time ingests 0 new docs (idempotency — this is the regression
  test to write).

## PR 2 — SFC (real parsing trap, budget more time)

- `core/sfc_client.py`: two listing pages — this is the whole corpus, no
  year range needed:
  - `https://www.sfc.hk/en/Rules-and-standards/Codes-and-guidelines/Codes`
  - `https://www.sfc.hk/en/Rules-and-standards/Codes-and-guidelines/Guidelines`
- **The trap:** each page's real document rows look like
  `<tr data-code-guideline-id="...">`. Further down the *same* page there
  are "previous versions" popup blocks containing their own `<tr>` rows —
  these lack the `data-code-guideline-id` attribute, and their single `<td>`
  is just a date range (e.g. "1 Oct 2013 - 31 Dec 2021"), not a document.
  **Only parse rows that carry `data-code-guideline-id`.** A naive "every
  `<tr>` with a `.pdf` link" approach inflates the Codes page to 99 rows and
  Guidelines to 108 — the real attribute-carrying rows number 12 and 51
  respectively; the rest is popup date-range junk.
- **The Handbook exception:** one Codes-page attribute row (the "SFC
  Handbook for Unit Trusts and Mutual Funds...") has no direct PDF — its
  date cell is a popup trigger (`<a class="popup-btn"
  data-popup-id="#popuphb-<id>">`) pointing at a "Latest version" popup
  block holding 5 PDFs: the full Handbook plus Sections I–IV. Ingest all
  5, one record each — title from the heading above each PDF's table (the
  Handbook title itself for the "Latest version" entry), issue_date from
  the link text. Only follow the popup referenced from the *date cell*;
  the "Previous versions" column's popup (`#popup<id>`, no `hb`) holds
  date-range junk and stays excluded.
- Title = the first `<td>` in the row (not the PDF link's own text, which is
  just a date like "2 Jan 2026").
- Add a zero-results guard: if either page yields 0 real rows, fail loudly.
  That means the page's HTML structure changed, not that there's nothing to
  ingest.
- **Acceptance:** Codes → 16 documents (11 direct-PDF rows + 5 from the
  Handbook's latest-version popup), Guidelines → 51 documents. Include a
  saved real-HTML fixture (trimmed, but keep the Handbook row with its
  `popuphb` block and a previous-versions popup block intact) as a
  regression test proving the latest-version popup PDFs are included and
  the previous-versions rows are excluded.

## Politeness

No source in this spec has shown rate-limiting so far, but add a small
delay between requests (~0.5–1s) and don't hammer any of these sites in a
tight loop.
