from __future__ import annotations

from typing import Any

BASE_URL = "https://www.ia.org.hk"
LISTING_URL_TMPL = (
    f"{BASE_URL}/en/legislative_framework/circulars/reg_matters/"
    "circulars_on_regulatory_matters_{year}.html"
)


class IAClient:
    """Discovers Insurance Authority "Circulars on Regulatory Matters" PDFs.

    See docs/SPEC.md "PR 2 — IA" for the page structure and expected counts.
    """

    def __init__(self, timeout_sec: int = 45):
        self.timeout_sec = timeout_sec

    def discover_documents(self, start_year: int, end_year: int) -> list[dict[str, Any]]:
        raise NotImplementedError("PR 2: implement IAClient.discover_documents")
