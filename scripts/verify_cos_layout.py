#!/usr/bin/env python
"""Read-only verification of COS layout for originals and Docling outputs.

This script lists counts for:
- pdfs/*.pdf
- transformed/*.md
- transformed/*.html

Bbox/layout data is stored in AstraDB — not as .metadata.json sidecars in COS.
It does not modify COS.

Usage
-----
    .venv/bin/python scripts/verify_cos_layout.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import ibm_boto3
from ibm_botocore.client import Config


_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("verify_cos_layout")

REQUIRED = ["COS_API_KEY", "COS_INSTANCE_CRN", "COS_ENDPOINT", "COS_BUCKET"]
missing = [name for name in REQUIRED if not os.environ.get(name)]
if missing:
    log.error("Missing required env vars: %s", ", ".join(missing))
    sys.exit(1)

cos = ibm_boto3.client(
    "s3",
    ibm_api_key_id=os.environ["COS_API_KEY"],
    ibm_service_instance_id=os.environ["COS_INSTANCE_CRN"],
    config=Config(signature_version="oauth"),
    endpoint_url=os.environ["COS_ENDPOINT"],
)
BUCKET = os.environ["COS_BUCKET"]


def list_keys(prefix: str) -> list[str]:
    paginator = cos.get_paginator("list_objects_v2")
    return [
        obj["Key"]
        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix)
        for obj in page.get("Contents", [])
    ]


def main() -> None:
    pdfs = [key for key in list_keys("pdfs/") if key.endswith(".pdf")]
    transformed_md = [key for key in list_keys("transformed/") if key.endswith(".md")]
    transformed_html = [key for key in list_keys("transformed/") if key.endswith(".html")]

    log.info("Bucket: %s", BUCKET)
    log.info("pdfs/*.pdf          %d", len(pdfs))
    log.info("transformed/*.md    %d", len(transformed_md))
    log.info("transformed/*.html  %d", len(transformed_html))
    log.info("(bbox/layout data is stored in AstraDB, not COS)")


if __name__ == "__main__":
    main()
