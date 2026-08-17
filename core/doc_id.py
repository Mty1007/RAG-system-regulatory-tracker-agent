from __future__ import annotations

import hashlib
from urllib.parse import urlparse

SOURCES = ("SFC", "PCPD")


def make_doc_id(source: str, download_url: str) -> str:
    """Stable, idempotent doc_id for a regulator document.

    Hashes the URL PATH only (never the full URL). Some sources (e.g. SFC)
    append a cache-busting query string like `?rev=<hash>` that changes when
    a document is republished at the same path — hashing the full URL would
    mint a new doc_id on every republish and break the "already ingested,
    skip" dedup check, silently re-ingesting the same document forever.
    """
    if source not in SOURCES:
        raise ValueError(
            f"Unknown source {source!r} — must be one of {SOURCES}"
        )
    path = urlparse(download_url).path
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]
    return f"{source.lower()}-{digest}"
