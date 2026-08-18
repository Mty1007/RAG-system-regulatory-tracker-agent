#!/usr/bin/env python
"""Audit COS bucket: list pdfs/*.pdf, compare against what the PCPD and SFC
clients would discover today, and print a full gap report.

Metadata records (docs/*.json) are no longer expected in COS — they were
removed in favour of the PDF-only store.  Bbox/layout data lives in AstraDB.

Usage:
    export COS_API_KEY="..."
    export COS_INSTANCE_CRN="..."
    export COS_ENDPOINT="..."
    export COS_BUCKET="..."
    .venv/bin/python scripts/audit_cos.py
"""
from __future__ import annotations

import logging
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import ibm_boto3
from ibm_botocore.client import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("audit_cos")

REQUIRED_ENV = ["COS_API_KEY", "COS_INSTANCE_CRN", "COS_ENDPOINT", "COS_BUCKET"]
missing_env = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if missing_env:
    log.error("Missing env vars: %s", ", ".join(missing_env))
    sys.exit(1)

cos = ibm_boto3.client(
    "s3",
    ibm_api_key_id=os.environ["COS_API_KEY"],
    ibm_service_instance_id=os.environ["COS_INSTANCE_CRN"],
    config=Config(signature_version="oauth"),
    endpoint_url=os.environ["COS_ENDPOINT"],
)
BUCKET = os.environ["COS_BUCKET"]


# ── helpers ───────────────────────────────────────────────────────────────────

def list_all_keys(prefix: str) -> list[str]:
    """Return every key under prefix using the paginator."""
    paginator = cos.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


# ── 1. List PDFs that are actually in COS ────────────────────────────────────

log.info("=" * 60)
log.info("STEP 1 — Listing pdfs/*.pdf in COS bucket '%s'", BUCKET)

all_pdf_keys = [k for k in list_all_keys("pdfs/") if k.endswith(".pdf")]
cos_pdf_ids = {k.removeprefix("pdfs/").removesuffix(".pdf") for k in all_pdf_keys}

log.info("  pdfs/*.pdf : %d", len(all_pdf_keys))

# Break down by source prefix
by_source_pdfs: dict[str, set[str]] = defaultdict(set)
for doc_id in cos_pdf_ids:
    src = doc_id.split("-")[0].upper()
    by_source_pdfs[src].add(doc_id)

log.info("")
log.info("  Breakdown by source (pdfs):")
for src in sorted(by_source_pdfs):
    log.info("    %-6s  %d", src, len(by_source_pdfs[src]))

# ── 2. Discover what the clients return today ─────────────────────────────────

log.info("")
log.info("STEP 2 — Discovering documents from live sources")

from core.pcpd_client import PCPDClient  # noqa: E402
from core.sfc_client  import SFCClient   # noqa: E402

log.info("  PCPD ...")
pcpd_docs = PCPDClient().discover_documents()
log.info("  SFC ...")
sfc_docs = SFCClient().discover_documents()

all_expected = pcpd_docs + sfc_docs
expected_ids = {d["doc_id"] for d in all_expected}

log.info("")
log.info("  Expected totals from live scrape:")
log.info("    PCPD : %d", len(pcpd_docs))
log.info("    SFC  : %d", len(sfc_docs))
log.info("    TOTAL: %d", len(all_expected))

# ── 3. Gap analysis ───────────────────────────────────────────────────────────

log.info("")
log.info("STEP 3 — Gap analysis (expected PDFs vs COS)")

not_in_cos_pdfs = expected_ids - cos_pdf_ids
in_cos_not_expected = cos_pdf_ids - expected_ids

if not_in_cos_pdfs:
    log.warning("  %d PDFs MISSING from COS:", len(not_in_cos_pdfs))
    for doc_id in sorted(not_in_cos_pdfs):
        rec = next((d for d in all_expected if d["doc_id"] == doc_id), {})
        log.warning("    NO PDF:  %s  %s", doc_id, rec.get("title", "?"))
else:
    log.info("  All expected PDFs are present in COS ✓")

if in_cos_not_expected:
    log.info(
        "  %d doc_ids in COS not matched by current scrape (stale/renamed):",
        len(in_cos_not_expected),
    )
    for doc_id in sorted(in_cos_not_expected):
        log.info("    EXTRA: %s", doc_id)

# ── 4. Summary ────────────────────────────────────────────────────────────────

log.info("")
log.info("=" * 60)
log.info("SUMMARY")
log.info("  COS pdfs   : %d", len(cos_pdf_ids))
log.info("  Expected   : %d", len(expected_ids))
log.info("  Missing pdf: %d", len(not_in_cos_pdfs))
log.info("  Extra (stale): %d", len(in_cos_not_expected))
exit_code = 1 if not_in_cos_pdfs else 0
log.info("  Status     : %s", "NEEDS FIX" if exit_code else "ALL GOOD ✓")
log.info("=" * 60)
sys.exit(exit_code)
