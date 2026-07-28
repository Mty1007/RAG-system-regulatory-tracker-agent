#!/usr/bin/env python
"""One-shot backfill: discover all PR 1-3 documents and push them to IBM COS.

For every document this script will:
  1. Download the PDF from download_url and upload it to COS under
     pdfs/<doc_id>.pdf  — ready for the OCR pipeline.

The script stores original files only; it does not write source metadata JSON
records into COS.
Both steps are idempotent: already-present PDFs are skipped.

Usage
-----
Set the five required env vars and run:

    export COS_API_KEY="..."
    export COS_INSTANCE_CRN="crn:v1:bluemix:..."
    export COS_ENDPOINT="https://s3.us-south.cloud-object-storage.appdomain.cloud"
    export COS_BUCKET="regulatory-tracker-docs"
    # optional: separate bucket for PDFs
    # export COS_PDF_BUCKET="regulatory-tracker-pdfs"

    .venv/bin/python scripts/backfill_cos.py

    # IA: choose which years to backfill (defaults to 2025-2026)
    IA_START_YEAR=2020 IA_END_YEAR=2026 .venv/bin/python scripts/backfill_cos.py
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

import requests

# ── logging setup ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill")

# ── env-var check ────────────────────────────────────────────────────────────

REQUIRED_ENV = ["COS_API_KEY", "COS_INSTANCE_CRN", "COS_ENDPOINT", "COS_BUCKET"]
missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if missing:
    log.error("Missing required environment variables: %s", ", ".join(missing))
    log.error("See the docstring at the top of this file for usage.")
    sys.exit(1)

# ── imports that need env-vars already set ───────────────────────────────────

# Add repo root to path when running as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.ia_client import IAClient          # noqa: E402
from core.pcpd_client import PCPDClient      # noqa: E402
from core.sfc_client import SFCClient        # noqa: E402
from store.cos_document_store import COSDocumentStore  # noqa: E402

# ── helpers ──────────────────────────────────────────────────────────────────

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = (
    "Mozilla/5.0 (compatible; regulatory-tracker-backfill/1.0)"
)


def _download_pdf(url: str, timeout: int = 30) -> bytes | None:
    """Download a PDF. Returns None on any non-2xx response."""
    try:
        resp = _SESSION.get(url, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and b"%PDF" in resp.content[:8]:
            return resp.content
        log.warning("Skipping PDF (HTTP %s): %s", resp.status_code, url)
        return None
    except requests.RequestException as exc:
        log.warning("PDF download failed for %s: %s", url, exc)
        return None


def _backfill_source(
    store: COSDocumentStore,
    name: str,
    docs: list[dict[str, Any]],
    pdf_delay: float = 0.5,
) -> dict[str, int]:
    """Push PDFs for one source. Returns counts."""
    counts = dict(pdf_new=0, pdf_skip=0, pdf_fail=0)

    for doc in docs:
        doc_id = doc["doc_id"]

        # ── PDF ──────────────────────────────────────────────────────────────
        pdf_url = doc.get("download_url", "")
        if ".pdf" not in pdf_url.lower():
            log.debug("No direct PDF URL for %s — skipping PDF upload", doc_id)
            counts["pdf_skip"] += 1
            continue

        if store.pdf_exists(doc_id):
            counts["pdf_skip"] += 1
        else:
            pdf_bytes = _download_pdf(pdf_url)
            if pdf_bytes:
                store.put_pdf(doc_id, pdf_bytes)
                counts["pdf_new"] += 1
            else:
                counts["pdf_fail"] += 1

        time.sleep(pdf_delay)  # polite delay between PDF downloads

    log.info(
        "[%s] pdf: +%d skipped=%d failed=%d",
        name,
        counts["pdf_new"], counts["pdf_skip"], counts["pdf_fail"],
    )
    return counts


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    store = COSDocumentStore()

    ia_start = int(os.environ.get("IA_START_YEAR", 2025))
    ia_end   = int(os.environ.get("IA_END_YEAR",   2026))

    sources: list[tuple[str, list[dict[str, Any]]]] = []

    log.info("=== PR 1 — PCPD (curated, 4 docs) ===")
    sources.append(("PCPD", PCPDClient().discover_documents()))

    log.info("=== PR 2 — IA (years %d–%d) ===", ia_start, ia_end)
    try:
        sources.append(("IA", IAClient().discover_documents(
            start_year=ia_start, end_year=ia_end
        )))
    except (ValueError, RuntimeError) as exc:
        log.warning("Skipping IA — discovery failed: %s", exc)

    log.info("=== PR 3 — SFC (Codes + Guidelines) ===")
    sources.append(("SFC", SFCClient().discover_documents()))

    totals: dict[str, int] = dict(pdf_new=0, pdf_skip=0, pdf_fail=0)

    for name, docs in sources:
        log.info("--- Backfilling %s (%d docs) ---", name, len(docs))
        counts = _backfill_source(store, name, docs)
        for k in totals:
            totals[k] += counts[k]

    log.info("=" * 60)
    log.info(
        "TOTAL  pdf: +%d skipped=%d failed=%d",
        totals["pdf_new"], totals["pdf_skip"], totals["pdf_fail"],
    )

    if totals["pdf_fail"]:
        log.warning(
            "%d PDFs could not be downloaded — re-run to retry.", totals["pdf_fail"]
        )
        sys.exit(2)

    log.info("Backfill complete.")


if __name__ == "__main__":
    main()
