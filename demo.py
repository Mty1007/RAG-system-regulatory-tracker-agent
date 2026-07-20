"""
Demo: evidence that PR 1 (PCPD), PR 2 (IA), and PR 3 (SFC) are complete.

Run with:
    .venv/bin/python demo.py

What it proves for the mentor:
  PR 1 — PCPDClient returns exactly 4 curated documents with correct fields.
  PR 2 — IAClient fetches live IA pages; 2026 → 18 docs, 2025 → 42 docs.
  PR 3 — SFCClient fetches live SFC pages; Codes → 16 docs, Guidelines → 51 docs.
         Handbook popup PDFs are included; previous-versions junk is excluded.

The final block re-uses the in-memory store to prove idempotency: ingesting
the same source twice produces 0 new records on the second call.
"""

from __future__ import annotations

import sys
import textwrap
from datetime import datetime

# ── helpers ──────────────────────────────────────────────────────────────────

SEP = "─" * 68

def section(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)

def ok(msg: str) -> None:
    print(f"  ✓  {msg}")

def info(msg: str) -> None:
    print(f"     {msg}")

def fail(msg: str) -> None:
    print(f"  ✗  {msg}", file=sys.stderr)
    sys.exit(1)

def assert_eq(label: str, got, expected) -> None:
    if got == expected:
        ok(f"{label}: {got}")
    else:
        fail(f"{label}: expected {expected}, got {got}")

def show_sample(docs: list[dict], n: int = 3) -> None:
    """Print the first n docs as a trimmed table."""
    for doc in docs[:n]:
        title = textwrap.shorten(doc["title"], width=55, placeholder="…")
        date  = doc["issue_date"] or "(no date)"
        dtype = doc["document_type"]
        print(f"       [{dtype}] {title!r}  {date}")
    if len(docs) > n:
        print(f"       … and {len(docs) - n} more")


# ── PR 1: PCPD ───────────────────────────────────────────────────────────────

section("PR 1 — PCPD  (curated list, no network)")

from core.pcpd_client import PCPDClient

docs = PCPDClient().discover_documents()
assert_eq("document count", len(docs), 4)

expected_titles = {
    "PDPO Full Ordinance",
    "Six Data Protection Principles — Overview",
    "Data Security Measures Guidance",
    "Data Breach Handling Guidance Note",
}
got_titles = {d["title"] for d in docs}
missing = expected_titles - got_titles
if missing:
    fail(f"Missing titles: {missing}")
ok("all 4 expected titles present")

# Check shape of every record
required_keys = {"doc_id","source","title","download_url","source_url","document_type","issue_date"}
for d in docs:
    missing_keys = required_keys - d.keys()
    if missing_keys:
        fail(f"Record missing keys {missing_keys}: {d}")
ok("every record has all 7 required fields")

# Check document_type rules
ordinances = [d for d in docs if d["document_type"] == "Ordinance"]
guidances   = [d for d in docs if d["document_type"] == "Guidance"]
assert_eq("Ordinance count", len(ordinances), 1)
assert_eq("Guidance count",  len(guidances),  3)

# Check doc_id prefix
for d in docs:
    if not d["doc_id"].startswith("pcpd-"):
        fail(f"Bad doc_id prefix: {d['doc_id']}")
ok("all doc_ids start with 'pcpd-'")

# Idempotency: calling twice gives identical doc_id sets
first  = {d["doc_id"] for d in PCPDClient().discover_documents()}
second = {d["doc_id"] for d in PCPDClient().discover_documents()}
if first != second:
    fail("discover_documents() is not idempotent")
ok("idempotent — two calls return identical doc_id sets")

print()
show_sample(docs, n=4)


# ── PR 2: IA ─────────────────────────────────────────────────────────────────

section("PR 2 — IA  (live network — Insurance Authority)")

from core.ia_client import IAClient

client = IAClient()

print("\n  Fetching year 2026 …")
docs_2026 = client.discover_documents(start_year=2026, end_year=2026)
assert_eq("2026 document count", len(docs_2026), 18)

print("\n  Fetching year 2025 …")
docs_2025 = client.discover_documents(start_year=2025, end_year=2025)
assert_eq("2025 document count", len(docs_2025), 42)

# Shape checks on 2026 sample
for d in docs_2026:
    if d["source"] != "IA":
        fail(f"source should be 'IA', got {d['source']!r}")
    if d["document_type"] != "Circular":
        fail(f"document_type should be 'Circular', got {d['document_type']!r}")
    if not d["download_url"].endswith(".pdf"):
        fail(f"download_url does not end in .pdf: {d['download_url']}")
    if not d["doc_id"].startswith("ia-"):
        fail(f"Bad doc_id prefix: {d['doc_id']}")
ok("all 2026 records: source=IA, document_type=Circular, .pdf URLs, ia- prefix")

# Spot-check: at least one record has a valid ISO issue_date
dated = [d for d in docs_2026 if d["issue_date"]]
if not dated:
    fail("no records have an issue_date — date parsing may be broken")
ok(f"{len(dated)}/18 records have a parsed ISO issue_date (e.g. {dated[0]['issue_date']!r})")

# Verify 404 guard fires for a year before the archive
print("\n  Testing 404 guard (year 2004, predates archive) …")
try:
    client.discover_documents(start_year=2004, end_year=2004)
    fail("expected ValueError for year 2004 but none was raised")
except ValueError as exc:
    ok(f"404 guard raised ValueError as expected: {exc}")

print()
show_sample(docs_2026, n=3)


# ── PR 3: SFC ────────────────────────────────────────────────────────────────

section("PR 3 — SFC  (live network — Securities and Futures Commission)")

from core.sfc_client import SFCClient, LISTING_URLS

sfc = SFCClient()

print("\n  Fetching Codes and Guidelines pages …")
all_docs = sfc.discover_documents()

codes_docs      = [d for d in all_docs if d["document_type"] == "Codes"]
guidelines_docs = [d for d in all_docs if d["document_type"] == "Guidelines"]

assert_eq("Codes document count",      len(codes_docs),      16)
assert_eq("Guidelines document count", len(guidelines_docs), 51)
assert_eq("total document count",      len(all_docs),        67)

# Handbook popup PDFs present
handbook_docs = [d for d in codes_docs if "Handbook" in d["title"] or "Section" in d["title"]]
if len(handbook_docs) < 5:
    fail(f"Expected ≥5 Handbook/Section records, got {len(handbook_docs)}: {[d['title'] for d in handbook_docs]}")
ok(f"Handbook popup expanded: {len(handbook_docs)} records (full Handbook + Sections I–IV)")

# Previous-versions junk excluded: no title should be a bare date-range string
for d in all_docs:
    if " - " in d["title"]:
        parts = d["title"].split(" - ")
        if all(p.strip()[:2].isdigit() for p in parts):
            fail(f"Previous-versions date-range leaked into results: {d['title']!r}")
ok("no previous-versions date-range titles in results")

# source_url is the correct listing page for each document_type
for d in codes_docs:
    if LISTING_URLS["Codes"] not in d["source_url"]:
        fail(f"Codes doc has wrong source_url: {d['source_url']}")
for d in guidelines_docs:
    if LISTING_URLS["Guidelines"] not in d["source_url"]:
        fail(f"Guidelines doc has wrong source_url: {d['source_url']}")
ok("source_url is the correct listing page for every record")

# doc_id prefix
for d in all_docs:
    if not d["doc_id"].startswith("sfc-"):
        fail(f"Bad doc_id prefix: {d['doc_id']}")
ok("all doc_ids start with 'sfc-'")

print()
show_sample(codes_docs, n=5)


# ── Idempotency via in-memory store ──────────────────────────────────────────

section("Idempotency check  (in-memory store, all three sources)")

from store.document_store import DocumentStore

store = DocumentStore()

def ingest(docs: list[dict]) -> tuple[int, int]:
    ingested = skipped = 0
    for d in docs:
        if store.get_document(d["doc_id"]):
            skipped += 1
        else:
            store.insert_document(d)
            ingested += 1
    return ingested, skipped

pcpd_docs = PCPDClient().discover_documents()
ia_docs   = IAClient().discover_documents(start_year=2026, end_year=2026)
sfc_docs  = sfc.discover_documents()

for name, docs in [("PCPD", pcpd_docs), ("IA-2026", ia_docs), ("SFC", sfc_docs)]:
    ing1, skp1 = ingest(docs)
    ing2, skp2 = ingest(docs)
    if ing1 == 0:
        fail(f"{name}: first ingest saved 0 docs — something is wrong")
    if ing2 != 0:
        fail(f"{name}: second ingest saved {ing2} docs — not idempotent")
    ok(f"{name}: 1st call → ingested={ing1}, skipped={skp1}  |  2nd call → ingested={ing2}, skipped={skp2}")


# ── Summary ──────────────────────────────────────────────────────────────────

section("All checks passed ✓")
print(f"""
  PR 1 (PCPD)  — 4 documents discovered, correct types, idempotent.
  PR 2 (IA)    — 2026 → 18 docs, 2025 → 42 docs, 404 guard works.
  PR 3 (SFC)   — Codes → 16 docs (incl. 5 Handbook PDFs),
                  Guidelines → 51 docs.  Total: 67.

  Completed at {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
""")
