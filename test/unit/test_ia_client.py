from pathlib import Path

import pytest

from core.ia_client import IAClient

FIXTURE = Path(__file__).parent / "fixtures" / "ia_circulars_sample.html"
SOURCE_URL = "https://www.ia.org.hk/en/legislative_framework/circulars/reg_matters/circulars_on_regulatory_matters_2025.html"


def _parse_fixture() -> list[dict]:
    html = FIXTURE.read_text(encoding="utf-8")
    return IAClient.parse_page(html, source_url=SOURCE_URL)


def test_parse_page_returns_all_pdfs():
    """Fixture has 3 PDF links across 2 tables; all 3 must be returned."""
    docs = _parse_fixture()
    assert len(docs) == 3, f"Expected 3 docs, got {len(docs)}"


def test_parse_page_record_shape():
    """Every returned record must have all required fields with correct values."""
    docs = _parse_fixture()
    for doc in docs:
        assert doc["source"] == "IA"
        assert doc["document_type"] == "Circular"
        assert doc["source_url"] == SOURCE_URL
        assert doc["doc_id"].startswith("ia-")
        assert doc["download_url"].startswith("https://")
        assert doc["title"]
        assert "issue_date" in doc


def test_parse_page_date_parsing():
    """Dates in the fixture must be parsed to ISO format."""
    docs = _parse_fixture()
    by_title = {d["title"]: d for d in docs}
    assert by_title["Circular on Risk Management Practices"]["issue_date"] == "2025-01-15"
    assert by_title["Circular on Conduct Requirements"]["issue_date"] == "2025-03-03"
    assert by_title["Circular on Policyholder Protection"]["issue_date"] == "2025-06-10"


def test_parse_page_correlates_dates_across_all_tables():
    """PDFs in the second <table> must still get their issue_date."""
    docs = _parse_fixture()
    by_title = {d["title"]: d for d in docs}
    assert by_title["Circular on Policyholder Protection"]["issue_date"] == "2025-06-10", (
        "PDF in the second <table> lost its date — correlation only walked the first table"
    )


def test_discover_documents_404_guard():
    """A year that predates the IA archive must raise ValueError, not silently return [].

    The IA site returns 403 (not 404) for years that predate the archive.
    """
    with pytest.raises(ValueError, match="HTTP 40"):
        IAClient().discover_documents(start_year=2004, end_year=2004)


def test_discover_documents_correlates_dates_across_all_tables(monkeypatch):
    """issue_date correlation must not be limited to the page's first <table>.

    discover_documents() finds `row_dates` by walking only `soup.find("table")`
    (the FIRST table on the page), so a PDF listed in a second table silently
    gets issue_date == "" instead of its real date.
    """
    html = """
    <html><body>
    <table>
      <tr><td>1 January 2026</td><td><a href="/first.pdf">First Circular</a></td></tr>
    </table>
    <table>
      <tr><td>2 January 2026</td><td><a href="/second.pdf">Second Circular</a></td></tr>
    </table>
    </body></html>
    """

    class FakeResponse:
        status_code = 200
        text = html

    monkeypatch.setattr(
        "core.ia_client.requests.get", lambda *a, **k: FakeResponse()
    )

    docs = IAClient().discover_documents(start_year=2026, end_year=2026)
    by_title = {d["title"]: d for d in docs}

    assert by_title["First Circular"]["issue_date"] == "2026-01-01"
    assert by_title["Second Circular"]["issue_date"] == "2026-01-02", (
        "PDF in the second <table> lost its date — correlation only walked the first table"
    )
