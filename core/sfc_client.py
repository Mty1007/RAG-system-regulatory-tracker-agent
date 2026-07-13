from __future__ import annotations

from typing import Any

BASE_URL = "https://www.sfc.hk"
LISTING_URLS = {
    "Codes": f"{BASE_URL}/en/Rules-and-standards/Codes-and-guidelines/Codes",
    "Guidelines": f"{BASE_URL}/en/Rules-and-standards/Codes-and-guidelines/Guidelines",
}


class SFCClient:
    """Discovers SFC Codes and Guidelines PDFs.

    See docs/SPEC.md "PR 3 — SFC" for the parsing trap (only rows with
    `data-code-guideline-id` are real documents) and expected counts.
    """

    def __init__(self, timeout_sec: int = 45):
        self.timeout_sec = timeout_sec

    def discover_documents(self) -> list[dict[str, Any]]:
        raise NotImplementedError("PR 3: implement SFCClient.discover_documents")

    @staticmethod
    def parse_listing_page(html: str, base_url: str, document_type: str) -> list[dict[str, Any]]:
        """Parse one listing page's HTML into normalized records.

        Split out from discover_documents() so it can be unit-tested against
        a saved HTML fixture (test/unit/fixtures/sfc_codes_sample.html)
        without live network access. See docs/SPEC.md PR 3 for the trap this
        must handle: only rows carrying `data-code-guideline-id` are real
        documents.
        """
        raise NotImplementedError("PR 3: implement SFCClient.parse_listing_page")
