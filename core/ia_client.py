from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from core.doc_id import make_doc_id

BASE_URL = "https://www.ia.org.hk"
LISTING_URL_TMPL = (
    f"{BASE_URL}/en/legislative_framework/circulars/reg_matters/"
    "circulars_on_regulatory_matters_{year}.html"
)

_DATE_FMTS = ("%d %B %Y", "%d %b %Y")


def _parse_date(text: str) -> str:
    """Return ISO date string if *text* matches 'D Month YYYY', else ''."""
    cleaned = text.strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


class IAClient:
    """Discovers Insurance Authority "Circulars on Regulatory Matters" PDFs.

    See docs/SPEC.md "PR 2 — IA" for the page structure and expected counts.
    """

    def __init__(self, timeout_sec: int = 45):
        self.timeout_sec = timeout_sec

    # Browser-like headers to avoid bot-protection 403s on ia.org.hk
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    def discover_documents(self, start_year: int, end_year: int) -> list[dict[str, Any]]:
        session = requests.Session()
        session.headers.update(self._HEADERS)
        results: list[dict[str, Any]] = []
        for i, year in enumerate(range(start_year, end_year + 1)):
            if i > 0:
                time.sleep(0.75)
            url = LISTING_URL_TMPL.format(year=year)
            resp = session.get(url, timeout=self.timeout_sec)
            if resp.status_code in (403, 404):
                raise ValueError(
                    f"IA circular archive has no data for {year}: "
                    f"HTTP {resp.status_code} (predates the archive)"
                )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"IA circular archive returned HTTP {resp.status_code} for {year}"
                )
            results.extend(self.parse_page(resp.text, source_url=url))
        return results

    @staticmethod
    def parse_page(html: str, source_url: str) -> list[dict[str, Any]]:
        """Parse one year's listing page HTML into normalised records.

        Split out from discover_documents() so it can be unit-tested against
        a saved HTML fixture without live network access.

        Each table row is expected to have the date in the first cell and one
        or more PDF links in the remaining cells. All <table> elements on the
        page are walked — not just the first one.
        """
        soup = BeautifulSoup(html, "html.parser")
        results: list[dict[str, Any]] = []

        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            date_text = cells[0].get_text(strip=True)
            for a in row.find_all("a", href=lambda h: h and h.endswith(".pdf")):
                href = a["href"]
                download_url = href if href.startswith("http") else urljoin(source_url, href)
                results.append({
                    "doc_id": make_doc_id("IA", download_url),
                    "source": "IA",
                    "title": a.get_text(strip=True),
                    "download_url": download_url,
                    "source_url": source_url,
                    "document_type": "Circular",
                    "issue_date": _parse_date(date_text),
                })

        if not results:
            raise RuntimeError(
                f"IA parser found 0 PDFs for {source_url} — page structure may have changed"
            )

        return results
