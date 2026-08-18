"""IBM Cloud Object Storage-backed document store.

COS holds two types of objects:

* ``pdfs/<doc_id>.pdf``              — original source PDFs (authoritative copy)
* ``transformed/<doc_id>.(md|html)`` — Docling readable output

Bbox/layout data produced by Docling is stored in AstraDB (IBM DataStax) as
structured rows keyed by (doc_id, element_id) — NOT as JSON sidecars in COS.

Required environment variables
-------------------------------
COS_API_KEY          IBM IAM API key for the COS instance
COS_INSTANCE_CRN     CRN of the COS service instance
COS_ENDPOINT         Regional / direct endpoint URL
                     e.g. https://s3.us-south.cloud-object-storage.appdomain.cloud
COS_BUCKET           Target bucket name

Optional
--------
COS_PDF_BUCKET       Separate bucket for raw PDF bytes (defaults to COS_BUCKET).
                     Set this if you want readable output and PDFs in different buckets.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import ibm_boto3
from ibm_botocore.client import Config
from ibm_botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def _make_client():
    """Build an ibm_boto3 S3 client from environment variables."""
    return ibm_boto3.client(
        "s3",
        ibm_api_key_id=os.environ["COS_API_KEY"],
        ibm_service_instance_id=os.environ["COS_INSTANCE_CRN"],
        config=Config(signature_version="oauth"),
        endpoint_url=os.environ["COS_ENDPOINT"],
    )


class COSDocumentStore:
    """IBM COS-backed document store.

    Stores raw PDF bytes under ``pdfs/<doc_id>.pdf`` in COS_PDF_BUCKET
    (falls back to COS_BUCKET).  Docling readable output is stored under
    ``transformed/<doc_id>.(md|html)``.

    Bbox/layout data belongs in AstraDB — not in this store.
    """

    def __init__(self) -> None:
        self._client = _make_client()
        self._bucket = os.environ["COS_BUCKET"]
        self._pdf_bucket = os.environ.get("COS_PDF_BUCKET", self._bucket)

    # ── document metadata ────────────────────────────────────────────────────

    def get_document(self, doc_id: str) -> Optional[dict[str, Any]]:
        try:
            obj = self._client.get_object(
                Bucket=self._bucket, Key=f"docs/{doc_id}.json"
            )
            return json.loads(obj["Body"].read())
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def insert_document(self, record: dict[str, Any]) -> None:
        record = dict(record)  # don't mutate the caller's dict
        record.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        self._client.put_object(
            Bucket=self._bucket,
            Key=f"docs/{record['doc_id']}.json",
            Body=json.dumps(record, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info("Stored doc metadata: %s", record["doc_id"])

    def list_documents(self, source: Optional[str] = None) -> list[dict[str, Any]]:
        paginator = self._client.get_paginator("list_objects_v2")
        prefix = "docs/"
        keys = [
            obj["Key"]
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix)
            for obj in page.get("Contents", [])
            if obj["Key"].endswith(".json")
        ]
        if source:
            # doc_id keys are like "pcpd-<hash>", "sfc-<hash>"
            keys = [k for k in keys if k.startswith(f"docs/{source.lower()}-")]

        docs: list[dict[str, Any]] = []
        for key in keys:
            try:
                obj = self._client.get_object(Bucket=self._bucket, Key=key)
                docs.append(json.loads(obj["Body"].read()))
            except ClientError:
                logger.warning("Could not read %s — skipping", key)
        return docs

    # ── raw PDF storage (used by the backfill / OCR pipeline) ────────────────

    def put_pdf(self, doc_id: str, pdf_bytes: bytes) -> str:
        """Upload raw PDF bytes.  Returns the COS object key."""
        key = f"pdfs/{doc_id}.pdf"
        self._client.put_object(
            Bucket=self._pdf_bucket,
            Key=key,
            Body=pdf_bytes,
            ContentType="application/pdf",
        )
        logger.info("Stored PDF: %s (%d bytes)", key, len(pdf_bytes))
        return key

    def pdf_exists(self, doc_id: str) -> bool:
        """Return True if a PDF has already been uploaded for this doc_id."""
        try:
            self._client.head_object(Bucket=self._pdf_bucket, Key=f"pdfs/{doc_id}.pdf")
            return True
        except ClientError:
            return False
