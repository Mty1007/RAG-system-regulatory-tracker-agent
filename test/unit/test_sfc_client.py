from pathlib import Path

from core.sfc_client import SFCClient

FIXTURE = Path(__file__).parent / "fixtures" / "sfc_codes_sample.html"
BASE_URL = "https://www.sfc.hk"

DIRECT_TITLES = {
    "Code of Conduct for Persons Licensed by or Registered with the Securities and Futures Commission",
    "Code of Conduct for Persons Providing Credit Rating Services",
}

HANDBOOK_TITLES = {
    "SFC Handbook for Unit Trusts and Mutual Funds, Investment-Linked Assurance Schemes and Unlisted Structured Investment Products",
    "Section I - Overarching Principles Section",
    "Section II - Code on Unit Trusts and Mutual Funds",
    "Section III - Code on Investment-Linked Assurance Schemes",
    "Section IV - Code on Unlisted Structured Investment Products",
}


def _parse_fixture() -> list[dict]:
    html = FIXTURE.read_text(encoding="utf-8")
    return SFCClient.parse_listing_page(html, base_url=BASE_URL, document_type="Code")


def test_direct_pdf_rows_parsed():
    """Fixture has 2 direct-PDF rows; both must appear in the result."""
    docs = _parse_fixture()
    returned_titles = {d["title"] for d in docs}
    assert DIRECT_TITLES.issubset(returned_titles), (
        f"Missing direct-row titles: {DIRECT_TITLES - returned_titles}"
    )
    # Exactly 2 docs match the known direct titles
    direct_docs = [d for d in docs if d["title"] in DIRECT_TITLES]
    assert len(direct_docs) == 2


def test_handbook_popup_pdfs_included():
    """Fixture Handbook popup yields exactly 5 PDF records."""
    docs = _parse_fixture()
    handbook_docs = [d for d in docs if d["title"] in HANDBOOK_TITLES]
    assert len(handbook_docs) == 5, (
        f"Expected 5 Handbook PDFs, got {len(handbook_docs)}: {[d['title'] for d in handbook_docs]}"
    )


def test_previous_versions_excluded():
    """No returned doc title may contain a date-range string like '16 Nov 2022 - 1 Jan 2026'."""
    docs = _parse_fixture()
    for doc in docs:
        assert " - " not in doc["title"] or not any(
            part.strip()[:2].isdigit() for part in doc["title"].split(" - ")
        ), f"Unexpected date-range title: {doc['title']!r}"
        # Stricter: the fixture's specific popup date-range must not appear
        assert "16 Nov 2022" not in doc["title"], (
            f"Previous-versions popup title leaked into results: {doc['title']!r}"
        )
