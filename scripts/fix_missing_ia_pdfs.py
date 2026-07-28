#!/usr/bin/env python
"""Find and re-upload any IA PDFs that are missing or empty in COS.

Usage:
    export COS_API_KEY="..."
    export COS_INSTANCE_CRN="..."
    export COS_ENDPOINT="..."
    export COS_BUCKET="..."
    .venv/bin/python scripts/fix_missing_ia_pdfs.py
"""
from __future__ import annotations

import logging
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import ibm_boto3
from ibm_botocore.client import Config
from ibm_botocore.exceptions import ClientError

from core.ia_client import IAClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fix_ia_pdfs")

REQUIRED_ENV = ["COS_API_KEY", "COS_INSTANCE_CRN", "COS_ENDPOINT", "COS_BUCKET"]
missing_env = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if missing_env:
    log.error("Missing env vars: %s", ", ".join(missing_env))
    sys.exit(1)

client = ibm_boto3.client(
    "s3",
    ibm_api_key_id=os.environ["COS_API_KEY"],
    ibm_service_instance_id=os.environ["COS_INSTANCE_CRN"],
    config=Config(signature_version="oauth"),
    endpoint_url=os.environ["COS_ENDPOINT"],
)
BUCKET = os.environ["COS_BUCKET"]

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = (
    "Mozilla/5.0 (compatible; regulatory-tracker-backfill/1.0)"
)


def get_pdf_size_in_cos(doc_id: str) -> int:
    """Return size in bytes of pdfs/<doc_id>.pdf in COS, or 0 if missing."""
    try:
        resp = client.head_object(Bucket=BUCKET, Key=f"pdfs/{doc_id}.pdf")
        return resp["ContentLength"]
    except ClientError:
        return 0


def upload_pdf(doc_id: str, url: str) -> bool:
    """Download from url and upload to COS. Returns True on success."""
    try:
        resp = _SESSION.get(url, timeout=60, allow_redirects=True)
        if resp.status_code == 200 and b"%PDF" in resp.content[:8]:
            client.put_object(
                Bucket=BUCKET,
                Key=f"pdfs/{doc_id}.pdf",
                Body=resp.content,
                ContentType="application/pdf",
            )
            log.info("Uploaded pdfs/%s.pdf (%d bytes)", doc_id, len(resp.content))
            return True
        log.warning("Bad response for %s: HTTP %s", url, resp.status_code)
        return False
    except Exception as exc:
        log.error("Failed %s: %s", url, exc)
        return False


def main() -> None:
    log.info("Discovering IA docs (2025-2026)...")
    ia_docs = IAClient().discover_documents(start_year=2025, end_year=2026)
    log.info("Found %d IA docs total", len(ia_docs))

    missing = []
    for doc in ia_docs:
        size = get_pdf_size_in_cos(doc["doc_id"])
        if size == 0:
            missing.append(doc)
            log.info("MISSING: %s  %s", doc["doc_id"], doc["title"])

    if not missing:
        log.info("All %d IA PDFs are present in COS — nothing to fix.", len(ia_docs))
        return

    log.info("Found %d missing IA PDFs — uploading now...", len(missing))
    fixed = failed = 0
    for doc in missing:
        if upload_pdf(doc["doc_id"], doc["download_url"]):
            fixed += 1
        else:
            failed += 1
        time.sleep(0.5)

    log.info("Done — fixed=%d  failed=%d", fixed, failed)
    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()
