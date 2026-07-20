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

    def discover_documents(self, start_year: int, end_year: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for i, year in enumerate(range(start_year, end_year + 1)):
            if i > 0:
                time.sleep(0.75)
            url = LISTING_URL_TMPL.format(year=year)
            resp = requests.get(url, timeout=self.timeout_sec)
            if resp.status_code == 404:
                raise ValueError(
                    f"IA circular archive has no data for {year}: HTTP 404 (predates the archive)"
                )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"IA circular archive returned HTTP {resp.status_code} for {year}"
                )

            soup = BeautifulSoup(resp.text, "html.parser")
            pdf_links = soup.find_all("a", href=lambda h: h and h.endswith(".pdf"))

            if not pdf_links:
                raise RuntimeError(
                    f"IA parser found 0 PDFs for {year} — page structure may have changed"
                )

            # Build a map from each row to its date text for issue_date parsing
            # Walk all rows, track current date
            row_dates: dict = {}
            table = soup.find("table")
            if table:
                for row in table.find_all("tr"):
                    cells = row.find_all(["td", "th"])
                    if cells:
                        date_text = cells[0].get_text(strip=True)
                        row_pdfs = row.find_all("a", href=lambda h: h and h.endswith(".pdf"))
                        for a in row_pdfs:
                            row_dates[id(a)] = date_text

            for a in pdf_links:
                href = a["href"]
                # Make absolute URL
                download_url = href if href.startswith("http") else urljoin(url, href)
                title = a.get_text(strip=True)
                date_text = row_dates.get(id(a), "")
                issue_date = _parse_date(date_text)
                results.append({
                    "doc_id": make_doc_id("IA", download_url),
                    "source": "IA",
                    "title": title,
                    "download_url": download_url,
                    "source_url": url,
                    "document_type": "Circular",
                    "issue_date": issue_date,
                })

        return results
