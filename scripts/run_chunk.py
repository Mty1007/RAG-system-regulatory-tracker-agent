#!/usr/bin/env python
"""Chunk + embed pipeline.

For every ``transformed/<doc_id>.md`` object in COS this script will:
  1. Download the Markdown from COS.
  2. Split it into chunks (heading-aware, sliding-window fallback).
  3. Embed each chunk via WatsonX (ibm/slate-30m-english-rtrvr-v2, 1536-dim).
  4. Write chunks + vectors to AstraDB ``chunks`` collection.
  5. Write a JSONL backup to COS under ``chunks/<doc_id>.jsonl``
     (no vectors — those are owned by AstraDB; the JSONL is for audit /
     re-processing).

All steps are idempotent — already-chunked docs are skipped unless
``FORCE_RECHUNK=1`` is set.

The original PDFs (``pdfs/``) and Docling output (``transformed/``) are
never modified.

Usage
-----
    .venv/bin/python scripts/run_chunk.py

Optional env overrides
----------------------
OCR_SOURCE       only process one source prefix: PCPD | IA | SFC
FORCE_RECHUNK    set to "1" to re-chunk docs that already have chunks
SKIP_CHUNK_STORE set to "1" to skip writing to AstraDB (dry-run)
"""

from __future__ import annotations

import json
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
log = logging.getLogger("run_chunk")

# ── env-var check ─────────────────────────────────────────────────────────────
REQUIRED = [
    "COS_API_KEY", "COS_INSTANCE_CRN", "COS_ENDPOINT", "COS_BUCKET",
    "WATSONX_API_KEY", "WATSONX_PROJECT_ID", "WATSONX_URL", "WATSONX_EMBED_MODEL",
    "ASTRA_DB_APPLICATION_TOKEN", "ASTRA_DB_API_ENDPOINT",
]
missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    log.error("Missing required env vars: %s", ", ".join(missing))
    log.error("Make sure your .env file is populated correctly.")
    sys.exit(1)

# ── path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ibm_boto3                                          # noqa: E402
from ibm_botocore.client import Config                    # noqa: E402
from ibm_botocore.exceptions import ClientError          # noqa: E402

from core.chunker import chunk_markdown                   # noqa: E402
from core.embedder import embed_texts                     # noqa: E402
from store.astra_chunk_store import AstraChunkStore       # noqa: E402
from store.astra_layout_store import AstraLayoutStore     # noqa: E402

# ── COS client ────────────────────────────────────────────────────────────────
cos = ibm_boto3.client(
    "s3",
    ibm_api_key_id=os.environ["COS_API_KEY"],
    ibm_service_instance_id=os.environ["COS_INSTANCE_CRN"],
    config=Config(signature_version="oauth"),
    endpoint_url=os.environ["COS_ENDPOINT"],
)
BUCKET = os.environ["COS_BUCKET"]


# ── helpers ───────────────────────────────────────────────────────────────────

def list_transformed_keys(source_filter: str | None) -> list[str]:
    """Return all transformed/<doc_id>.md keys, optionally filtered by source."""
    paginator = cos.get_paginator("list_objects_v2")
    keys = [
        obj["Key"]
        for page in paginator.paginate(Bucket=BUCKET, Prefix="transformed/")
        for obj in page.get("Contents", [])
        if obj["Key"].endswith(".md")
    ]
    if source_filter:
        prefix = f"transformed/{source_filter.lower()}-"
        keys = [k for k in keys if k.startswith(prefix)]
    return keys


def download_markdown(key: str) -> str:
    """Download and decode a Markdown file from COS."""
    return cos.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")


def get_doc_metadata(doc_id: str) -> dict:
    """Read doc metadata from COS docs/<doc_id>.json for source + page info."""
    try:
        obj = cos.get_object(Bucket=BUCKET, Key=f"docs/{doc_id}.json")
        return json.loads(obj["Body"].read())
    except ClientError:
        return {}


def resolve_page_starts(
    chunks: list[dict],
    layout_elements: list[dict],
) -> None:
    """Inject page_start into each chunk in-place using layout element text.

    For each chunk, find the lowest page number among layout elements whose
    text appears as a substring of the chunk text.  Falls back to 0 if no
    element text matches (e.g. layout store was skipped for this doc).

    Layout elements are sorted by page ascending so we stop at the first match
    per chunk, which is the earliest page the chunk text appears on.
    """
    # sort elements by page so we find the earliest page first
    by_page = sorted(layout_elements, key=lambda e: e.get("page", 0))

    for chunk in chunks:
        chunk_text = chunk.get("text", "")
        page = 0
        for el in by_page:
            el_text = el.get("text", "").strip()
            if el_text and el_text in chunk_text:
                page = el.get("page", 0)
                break
        chunk["page_start"] = page


def jsonl_exists(doc_id: str) -> bool:
    """Return True if COS chunks/<doc_id>.jsonl already exists."""
    try:
        cos.head_object(Bucket=BUCKET, Key=f"chunks/{doc_id}.jsonl")
        return True
    except ClientError:
        return False


def write_jsonl_backup(doc_id: str, chunks: list[dict]) -> None:
    """Write chunk metadata (no vectors) to COS as chunks/<doc_id>.jsonl."""
    lines = [json.dumps(c, ensure_ascii=False) for c in chunks]
    body = "\n".join(lines).encode("utf-8")
    cos.put_object(
        Bucket=BUCKET,
        Key=f"chunks/{doc_id}.jsonl",
        Body=body,
        ContentType="application/x-ndjson; charset=utf-8",
    )
    log.info("  COS backup: chunks/%s.jsonl (%d chunks)", doc_id, len(chunks))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    source_filter  = os.environ.get("OCR_SOURCE")
    force_rechunk  = os.environ.get("FORCE_RECHUNK", "0") == "1"
    skip_astra     = os.environ.get("SKIP_CHUNK_STORE", "0") == "1"

    if source_filter:
        log.info("Source filter: %s", source_filter)
    if force_rechunk:
        log.info("FORCE_RECHUNK=1 — already-chunked docs will be re-processed")
    if skip_astra:
        log.info("SKIP_CHUNK_STORE=1 — AstraDB writes disabled (dry-run)")

    chunk_store: AstraChunkStore | None = None
    layout_store: AstraLayoutStore | None = None
    if not skip_astra:
        chunk_store = AstraChunkStore()
        layout_store = AstraLayoutStore()
        log.info("AstraDB chunk store and layout store connected")

    md_keys = list_transformed_keys(source_filter)
    log.info("Found %d Markdown files to process", len(md_keys))

    done = skipped = failed = 0

    try:
        for key in md_keys:
            doc_id = key.removeprefix("transformed/").removesuffix(".md")

            # idempotency check — skip if JSONL backup already exists
            if not force_rechunk and jsonl_exists(doc_id):
                log.info("SKIP  %s (already chunked)", doc_id)
                skipped += 1
                continue

            try:
                log.info("CHUNK %s ...", doc_id)

                # ── 1. download MD ────────────────────────────────────────────
                markdown = download_markdown(key)

                # ── 2. get source from doc metadata ──────────────────────────
                meta   = get_doc_metadata(doc_id)
                source = meta.get("source", doc_id.split("-")[0].upper())

                # ── 3. chunk ─────────────────────────────────────────────────
                chunks = chunk_markdown(doc_id, markdown)
                if not chunks:
                    log.warning("  WARN %s produced 0 chunks — skipping", doc_id)
                    skipped += 1
                    continue

                # ── 3b. resolve page_start from layout elements ───────────────
                layout_elements: list[dict] = []
                if layout_store is not None:
                    try:
                        layout_elements = layout_store.get_elements(doc_id)
                    except Exception as exc:
                        log.warning("  WARN could not fetch layout elements for %s: %s", doc_id, exc)

                resolve_page_starts(chunks, layout_elements)

                # inject source into every chunk
                for c in chunks:
                    c["source"] = source

                # ── 4. embed ──────────────────────────────────────────────────
                texts   = [c["text"] for c in chunks]
                vectors = embed_texts(texts)

                # ── 5a. write to AstraDB ──────────────────────────────────────
                if chunk_store is not None:
                    chunk_store.upsert_chunks(chunks, vectors)

                # ── 5b. write JSONL backup to COS ─────────────────────────────
                write_jsonl_backup(doc_id, chunks)

                log.info(
                    "  OK  %s  (%d chunks, %d words avg)",
                    doc_id,
                    len(chunks),
                    sum(c["token_count"] for c in chunks) // max(len(chunks), 1),
                )
                done += 1

            except Exception as exc:
                log.error("  FAIL %s: %s", doc_id, exc)
                failed += 1

            time.sleep(0.2)  # polite delay between WatsonX embed calls
    finally:
        if layout_store is not None:
            layout_store.close()

    log.info("=" * 60)
    log.info("TOTAL  processed=%d  skipped=%d  failed=%d", done, skipped, failed)

    if failed:
        log.warning("%d docs failed — re-run to retry.", failed)
        sys.exit(2)

    log.info("Chunk pipeline complete.")


if __name__ == "__main__":
    main()
