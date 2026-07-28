#!/usr/bin/env python
"""OCR Pipeline: read PDFs from COS, extract text + layout via Docling (local),
write readable output back to COS and bbox/layout elements to AstraDB.

For every pdfs/<doc_id>.pdf in COS this script will:
  1. Download the PDF bytes from COS.
  2. Convert with the local Docling Python package (no HTTP API needed).
  3. Write the transformed markdown to COS under transformed/<doc_id>.md.
  4. Store all bbox/layout elements (coordinates, page, type, text) as structured
     rows in AstraDB — keyed by (doc_id, element_id).

The original PDF remains unchanged in COS under pdfs/<doc_id>.pdf.
Bbox/layout data is NOT written as a JSON sidecar in COS; it belongs in AstraDB
as structured columns that downstream queries can filter and index efficiently.
All steps are idempotent — already-processed docs are skipped.

Usage
-----
Make sure .env is populated, then run:

    .venv/bin/python scripts/run_ocr.py

Optional env overrides
----------------------
OCR_SOURCE        only process one source prefix: PCPD | IA | SFC
                  e.g.  OCR_SOURCE=PCPD .venv/bin/python scripts/run_ocr.py
SKIP_LAYOUT_STORE set to "1" to skip writing to AstraDB (dry-run mode)
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
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
    "ASTRA_DB_APPLICATION_TOKEN", "ASTRA_DB_API_ENDPOINT",
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
from store.astra_layout_store import AstraLayoutStore  # noqa: E402

try:
    from docling.document_converter import DocumentConverter  # noqa: E402
except ImportError:
    log.error("docling is not installed. Run: pip install docling")
    sys.exit(1)

# ── COS client ────────────────────────────────────────────────────────────────
cos = ibm_boto3.client(
    "s3",
    ibm_api_key_id=os.environ["COS_API_KEY"],
    ibm_service_instance_id=os.environ["COS_INSTANCE_CRN"],
    config=Config(signature_version="oauth"),
    endpoint_url=os.environ["COS_ENDPOINT"],
)
BUCKET = os.environ["COS_BUCKET"]

# ── Docling converter (initialised once, reused for all docs) ─────────────────
_converter = DocumentConverter()


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


def download_pdf(key: str) -> bytes:
    """Download PDF bytes from COS."""
    return cos.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def convert_pdf(pdf_bytes: bytes, filename: str) -> tuple[str, list[dict]]:
    """Convert PDF bytes using the local Docling package.

    Writes bytes to a temp file (Docling requires a file path), converts,
    then cleans up.

    Returns
    -------
    content   — Markdown string of the full document
    elements  — flat list of layout element dicts, each with:
                element_id, page, element_type, bbox [x0,y0,x1,y1], text
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        result = _converter.convert(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    doc = result.document

    # ── extract markdown ──────────────────────────────────────────────────────
    content = doc.export_to_markdown()
    if not content:
        raise RuntimeError(f"Docling produced empty markdown for {filename}")

    # ── extract layout elements ───────────────────────────────────────────────
    elements: list[dict] = []
    for idx, item in enumerate(doc.texts):
        # Each item has .text, .prov (provenance list with page + bbox info)
        text = getattr(item, "text", "") or ""
        prov_list = getattr(item, "prov", []) or []
        prov = prov_list[0] if prov_list else None

        page = 0
        bbox = [0.0, 0.0, 0.0, 0.0]
        if prov is not None:
            page = int(getattr(prov, "page_no", 0))
            raw_bbox = getattr(prov, "bbox", None)
            if raw_bbox is not None:
                bbox = [
                    float(getattr(raw_bbox, "l", 0.0)),
                    float(getattr(raw_bbox, "t", 0.0)),
                    float(getattr(raw_bbox, "r", 0.0)),
                    float(getattr(raw_bbox, "b", 0.0)),
                ]

        element_type = type(item).__name__

        elements.append({
            "element_id": str(idx),
            "page":         page,
            "element_type": element_type,
            "bbox":         bbox,
            "text":         text,
        })

    return content, elements


def write_transformed_content(doc_id: str, content: str) -> None:
    """Write Markdown output to COS as transformed/<doc_id>.md."""
    cos.put_object(
        Bucket=BUCKET,
        Key=f"transformed/{doc_id}.md",
        Body=content.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    source_filter = os.environ.get("OCR_SOURCE")
    skip_layout   = os.environ.get("SKIP_LAYOUT_STORE", "0") == "1"

    if source_filter:
        log.info("Source filter: %s", source_filter)

    layout_store: AstraLayoutStore | None = None
    if not skip_layout:
        layout_store = AstraLayoutStore()
        log.info("AstraDB layout store connected")
    else:
        log.info("SKIP_LAYOUT_STORE=1 — bbox/layout will NOT be written to AstraDB")

    pdf_keys = list_pdf_keys(source_filter)
    log.info("Found %d PDFs to process", len(pdf_keys))

    done = skipped = failed = 0

    try:
        for key in pdf_keys:
            doc_id = key.removeprefix("pdfs/").removesuffix(".pdf")

            if transformed_already_done(doc_id):
                log.info("SKIP  %s (already processed)", doc_id)
                skipped += 1
                continue

            try:
                log.info("OCR   %s ...", doc_id)
                pdf_bytes = download_pdf(key)
                content, elements = convert_pdf(pdf_bytes, filename=f"{doc_id}.pdf")
                write_transformed_content(doc_id, content)
                if layout_store is not None:
                    layout_store.insert_elements(doc_id, elements)
                log.info(
                    "  OK  %s  (%d chars, %d layout elements)",
                    doc_id, len(content), len(elements),
                )
                done += 1
            except Exception as exc:
                log.error("  FAIL %s: %s", doc_id, exc)
                failed += 1

            time.sleep(0.1)  # small pause between docs
    finally:
        if layout_store is not None:
            layout_store.close()

    log.info("=" * 60)
    log.info("TOTAL  processed=%d  skipped=%d  failed=%d", done, skipped, failed)

    if failed:
        log.warning("%d docs failed — re-run to retry.", failed)
        sys.exit(2)

    log.info("OCR complete.")


if __name__ == "__main__":
    main()
