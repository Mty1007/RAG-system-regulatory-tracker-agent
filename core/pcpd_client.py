from __future__ import annotations

from typing import Any

# TODO(PR 1): fill in the 4 known PCPD documents listed in docs/SPEC.md.
# No scraping needed — this is a fixed, curated list, not a discoverable page.
_KNOWN_DOCUMENTS: list[dict[str, str]] = []


class PCPDClient:
    """Returns the fixed set of PCPD/DPO compliance reference PDFs.

    See docs/SPEC.md "PR 1 — PCPD" for the exact documents and normalized
    record shape each entry must produce.
    """

    def discover_documents(self) -> list[dict[str, Any]]:
        raise NotImplementedError("PR 1: implement PCPDClient.discover_documents")
