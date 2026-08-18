#!/usr/bin/env python
"""Delete source document JSON records from IBM COS.

This script removes only docs/*.json objects from the configured COS bucket.
It does not delete transformed metadata sidecars such as
transformed/<doc_id>.metadata.json.

Usage
-----
Dry run (default):

    .venv/bin/python scripts/delete_source_json_from_cos.py

Delete matching objects:

    CONFIRM_DELETE=yes .venv/bin/python scripts/delete_source_json_from_cos.py
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
log = logging.getLogger("delete_source_json_from_cos")

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


def list_source_json_keys() -> list[str]:
    paginator = cos.get_paginator("list_objects_v2")
    return [
        obj["Key"]
        for page in paginator.paginate(Bucket=BUCKET, Prefix="docs/")
        for obj in page.get("Contents", [])
        if obj["Key"].endswith(".json")
    ]


def main() -> None:
    keys = list_source_json_keys()
    if not keys:
        log.info("No docs/*.json objects found in COS.")
        return

    log.info("Found %d docs/*.json object(s).", len(keys))
    for key in keys:
        log.info("  %s", key)

    if os.environ.get("CONFIRM_DELETE") != "yes":
        log.info("Dry run only. Set CONFIRM_DELETE=yes to delete these objects.")
        return

    deleted = 0
    for key in keys:
        cos.delete_object(Bucket=BUCKET, Key=key)
        deleted += 1
        log.info("Deleted %s", key)

    log.info("Deleted %d docs/*.json object(s).", deleted)


if __name__ == "__main__":
    main()
