#!/usr/bin/env python
"""OCR Pipeline: read PDFs from COS, extract text via watsonx Docling (TextExtractionsV2),
write Markdown output back to COS.

For every pdfs/<doc_id>.pdf in COS this script will:
  1. Confirm the PDF exists in the COS bucket registered as a watsonx connection asset.
  2. Submit a TextExtractionsV2 job (input=pdfs/<doc_id>.pdf, output=transformed/<doc_id>/).
  3. Poll until the job reaches a terminal state.
  4. The watsonx service writes the Markdown result directly to COS under
     transformed/<doc_id>/<doc_id>.md — the script renames it to the canonical
     transformed/<doc_id>.md key.

The original PDF remains unchanged in COS under pdfs/<doc_id>.pdf.
Layout/bbox data from the watsonx extraction is NOT stored locally — the
SKIP_LAYOUT_STORE flag is retained for forward-compatibility but layout writing
is not implemented in this version (watsonx does not expose raw bbox elements
via the TextExtractionsV2 API surface).
All steps are idempotent — already-processed docs are skipped.

Usage
-----
Make sure .env is populated, then run:

    .venv/bin/python scripts/run_ocr.py

Optional env overrides
----------------------
OCR_SOURCE        only process one source prefix: PCPD | SFC
                  e.g.  OCR_SOURCE=PCPD .venv/bin/python scripts/run_ocr.py
SKIP_LAYOUT_STORE retained for compatibility; bbox data is not written in this version
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

# ── load .env before anything else ───────────────────────────────────────────
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("run_ocr")

# ── env-var check ─────────────────────────────────────────────────────────────
REQUIRED = [
    "COS_API_KEY", "COS_INSTANCE_CRN", "COS_ENDPOINT", "COS_BUCKET",
    "WATSONX_API_KEY", "WATSONX_URL", "WATSONX_PROJECT_ID",
    "WATSONX_COS_CONNECTION_ASSET_ID",
]
missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    log.error("Missing required env vars: %s", ", ".join(missing))
    log.error("Make sure your .env file is populated correctly.")
    sys.exit(1)

# ── imports ───────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ibm_boto3                                       # noqa: E402
from ibm_botocore.client import Config                 # noqa: E402
from ibm_botocore.exceptions import ClientError        # noqa: E402

from ibm_watsonx_ai import Credentials                 # noqa: E402
from ibm_watsonx_ai.foundation_models.extractions import (  # noqa: E402
    TextExtractionsV2,
    TextExtractionsV2ResultFormats,
)
from ibm_watsonx_ai.helpers import DataConnection, S3Location  # noqa: E402

# ── constants ─────────────────────────────────────────────────────────────────
BUCKET                   = os.environ["COS_BUCKET"]
WATSONX_PROJECT_ID       = os.environ["WATSONX_PROJECT_ID"]
CONNECTION_ASSET_ID      = os.environ["WATSONX_COS_CONNECTION_ASSET_ID"]

# How long (seconds) to wait between job-status polls
_POLL_INTERVAL = 5
# Maximum total seconds to wait for a single job before giving up
_JOB_TIMEOUT   = 600

# ── COS client (for skip checks and result key rename) ────────────────────────
cos = ibm_boto3.client(
    "s3",
    ibm_api_key_id=os.environ["COS_API_KEY"],
    ibm_service_instance_id=os.environ["COS_INSTANCE_CRN"],
    config=Config(signature_version="oauth"),
    endpoint_url=os.environ["COS_ENDPOINT"],
)

# ── watsonx TextExtractionsV2 client ─────────────────────────────────────────
_extraction = TextExtractionsV2(
    credentials=Credentials(
        api_key=os.environ["WATSONX_API_KEY"],
        url=os.environ["WATSONX_URL"],
    ),
    project_id=WATSONX_PROJECT_ID,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def list_pdf_keys(source_filter: str | None) -> list[str]:
    """Return all pdfs/<doc_id>.pdf keys in the bucket, optionally filtered by source prefix."""
    paginator = cos.get_paginator("list_objects_v2")
    keys = [
        obj["Key"]
        for page in paginator.paginate(Bucket=BUCKET, Prefix="pdfs/")
        for obj in page.get("Contents", [])
        if obj["Key"].endswith(".pdf")
    ]
    if source_filter:
        prefix = f"pdfs/{source_filter.lower()}-"
        keys = [k for k in keys if k.startswith(prefix)]
    return keys


def transformed_already_done(doc_id: str) -> bool:
    """Return True if transformed/<doc_id>.md already exists in COS."""
    try:
        cos.head_object(Bucket=BUCKET, Key=f"transformed/{doc_id}.md")
        return True
    except ClientError:
        return False


def _poll_job(job_id: str) -> str:
    """Poll until the job reaches 'completed' or 'failed'. Returns terminal status string."""
    elapsed = 0
    while elapsed < _JOB_TIMEOUT:
        details = _extraction.get_job_details(job_id)
        status = (
            details.get("entity", {})
            .get("results", {})
            .get("status", "unknown")
        )
        if status in ("completed", "failed", "canceled"):
            return status
        log.debug("  job %s status=%s (%.0fs elapsed)", job_id, status, elapsed)
        time.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL
    return "timeout"


def _watsonx_output_key(doc_id: str) -> str:
    """The key watsonx writes Markdown results to inside the output directory."""
    # watsonx writes: transformed/<doc_id>/assembly.md
    return f"transformed/{doc_id}/assembly.md"


def _promote_result(doc_id: str) -> str:
    """Copy watsonx output to the canonical key and delete the intermediate one.

    watsonx writes to:  transformed/<doc_id>/<doc_id>.md
    Canonical target:   transformed/<doc_id>.md
    """
    src_key = _watsonx_output_key(doc_id)
    dst_key = f"transformed/{doc_id}.md"

    # Read the result written by watsonx
    body = cos.get_object(Bucket=BUCKET, Key=src_key)["Body"].read()

    # Write to canonical key
    cos.put_object(
        Bucket=BUCKET,
        Key=dst_key,
        Body=body,
        ContentType="text/markdown; charset=utf-8",
    )

    # Clean up the intermediate directory key
    cos.delete_object(Bucket=BUCKET, Key=src_key)

    return body.decode("utf-8", errors="replace")


def run_extraction(doc_id: str) -> str:
    """Submit a TextExtractionsV2 job for *doc_id* and return the Markdown content.

    Parameters
    ----------
    doc_id : str
        Bare doc ID (no prefix/suffix), e.g. ``sfc-abc123``.

    Returns
    -------
    str
        The extracted Markdown text.

    Raises
    ------
    RuntimeError
        If the job fails, times out, or produces empty output.
    """
    input_ref = DataConnection(
        connection_asset_id=CONNECTION_ASSET_ID,
        location=S3Location(bucket=BUCKET, path=f"pdfs/{doc_id}.pdf"),
    )
    # Output path must end with / — watsonx writes files inside this directory.
    output_ref = DataConnection(
        connection_asset_id=CONNECTION_ASSET_ID,
        location=S3Location(bucket=BUCKET, path=f"transformed/{doc_id}/"),
    )

    job_details = _extraction.run_job(
        document_reference=input_ref,
        results_reference=output_ref,
        result_formats=[TextExtractionsV2ResultFormats.MARKDOWN],
    )
    job_id = TextExtractionsV2.get_job_id(job_details)
    log.info("  submitted job %s", job_id)

    status = _poll_job(job_id)

    if status != "completed":
        raise RuntimeError(f"TextExtractionsV2 job {job_id} ended with status={status!r}")

    content = _promote_result(doc_id)
    if not content.strip():
        raise RuntimeError(f"TextExtractionsV2 produced empty Markdown for {doc_id}")

    return content


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    source_filter = os.environ.get("OCR_SOURCE")
    skip_layout   = os.environ.get("SKIP_LAYOUT_STORE", "0") == "1"

    if source_filter:
        log.info("Source filter: %s", source_filter)
    if skip_layout:
        log.info("SKIP_LAYOUT_STORE=1 — bbox/layout data will not be written")

    pdf_keys = list_pdf_keys(source_filter)
    log.info("Found %d PDFs to process", len(pdf_keys))

    done = skipped = failed = 0

    for key in pdf_keys:
        doc_id = key.removeprefix("pdfs/").removesuffix(".pdf")

        if transformed_already_done(doc_id):
            log.info("SKIP  %s (already processed)", doc_id)
            skipped += 1
            continue

        try:
            log.info("OCR   %s ...", doc_id)
            content = run_extraction(doc_id)
            log.info("  OK  %s  (%d chars)", doc_id, len(content))
            done += 1
        except Exception as exc:
            log.error("  FAIL %s: %s", doc_id, exc)
            failed += 1

        time.sleep(0.1)  # small pause between submissions

    log.info("=" * 60)
    log.info("TOTAL  processed=%d  skipped=%d  failed=%d", done, skipped, failed)

    if failed:
        log.warning("%d docs failed — re-run to retry.", failed)
        sys.exit(2)

    log.info("OCR complete.")


if __name__ == "__main__":
    main()
