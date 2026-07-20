from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from core.doc_id import make_doc_id

BASE_URL = "https://www.sfc.hk"
LISTING_URLS = {
    "Codes": f"{BASE_URL}/en/Rules-and-standards/Codes-and-guidelines/Codes",
    "Guidelines": f"{BASE_URL}/en/Rules-and-standards/Codes-and-guidelines/Guidelines",
}

_DATE_FMTS = ("%d %b %Y", "%d %B %Y", "%b %Y", "%B %Y")


def _parse_date(text: str) -> str:
    """Return ISO date string if *text* is a recognisable date, else ''."""
    cleaned = text.strip()
    for fmt in _DATE_FMTS:
        try:
            dt = datetime.strptime(cleaned, fmt)
            if "%d" in fmt:
                return dt.strftime("%Y-%m-%d")
            return dt.strftime("%Y-%m")
        except ValueError:
            continue
    return ""


class SFCClient:
    """Discovers SFC Codes and Guidelines PDFs.

    See docs/SPEC.md "PR 3 — SFC" for the parsing trap (only rows with
    `data-code-guideline-id` are real documents) and expected counts.
    """

    def __init__(self, timeout_sec: int = 45):
        self.timeout_sec = timeout_sec

    def discover_documents(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for i, (document_type, url) in enumerate(LISTING_URLS.items()):
            if i > 0:
                time.sleep(0.75)
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            }
            resp = requests.get(url, timeout=self.timeout_sec, headers=headers)
            resp.raise_for_status()
            results.extend(
                self.parse_listing_page(resp.text, BASE_URL, document_type, source_url=url)
            )
        return results

    @staticmethod
    def parse_listing_page(
        html: str, base_url: str, document_type: str, source_url: str = ""
    ) -> list[dict[str, Any]]:
        """Parse one listing page's HTML into normalized records.

        Split out from discover_documents() so it can be unit-tested against
        a saved HTML fixture (test/unit/fixtures/sfc_codes_sample.html)
        without live network access. See docs/SPEC.md PR 3 for the trap this
        must handle: only rows carrying `data-code-guideline-id` are real
        documents.
        """
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("tr[data-code-guideline-id]")
        if not rows:
            raise RuntimeError(
                "SFC parser found 0 real rows — page structure may have changed"
            )

        results: list[dict[str, Any]] = []

        for row in rows:
            tds = row.find_all("td")
            if not tds:
                continue
            title = tds[0].get_text(strip=True)
            date_td = tds[1] if len(tds) > 1 else None
            if date_td is None:
                continue

            # Check for Handbook latest-version popup (#popuphb-...)
            popup_a = date_td.find("a", class_="popup-btn")
            if popup_a:
                popup_id = popup_a.get("data-popup-id", "")
                if popup_id.startswith("#popuphb-"):
                    # Follow the Handbook popup
                    block_id = popup_id.lstrip("#")
                    popup_block = soup.find(id=block_id)
                    if popup_block:
                        results.extend(
                            _extract_handbook_popup_records(
                                popup_block, title, base_url, document_type, source_url
                            )
                        )
                # If popup_id starts with #popup (no hb) → previous-versions junk, skip
                continue

            # Direct PDF in date cell
            direct_a = date_td.find("a", href=lambda h: h and ".pdf" in h)
            if direct_a:
                href = direct_a["href"]
                download_url = href if href.startswith("http") else urljoin(base_url, href)
                issue_date = _parse_date(direct_a.get_text(strip=True))
                results.append({
                    "doc_id": make_doc_id("SFC", download_url),
                    "source": "SFC",
                    "title": title,
                    "download_url": download_url,
                    "source_url": source_url,
                    "document_type": document_type,
                    "issue_date": issue_date,
                })

        return results


def _extract_handbook_popup_records(
    popup_block: Any,
    row_title: str,
    base_url: str,
    document_type: str,
    source_url: str = "",
) -> list[dict[str, Any]]:
    """Emit one record per PDF in a #popuphb-... Handbook popup block.

    The first PDF (before any <h4>) gets the row's own title.
    Each subsequent PDF is preceded by an <h4> whose text becomes the title.
    """
    records: list[dict[str, Any]] = []
    # Walk children to track current heading
    current_title = row_title  # for the first PDF (no preceding h4)
    for element in popup_block.descendants:
        if element.name == "h4":
            current_title = element.get_text(strip=True)
        elif element.name == "a":
            href = element.get("href", "")
            if ".pdf" not in href:
                continue
            download_url = href if href.startswith("http") else urljoin(base_url, href)
            issue_date = _parse_date(element.get_text(strip=True))
            records.append({
                "doc_id": make_doc_id("SFC", download_url),
                "source": "SFC",
                "title": current_title,
                "download_url": download_url,
                "source_url": source_url,
                "document_type": document_type,
                "issue_date": issue_date,
            })
    return records
