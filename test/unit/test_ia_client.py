import pytest

from core.ia_client import IAClient


def test_discover_2026_count():
    docs = IAClient().discover_documents(start_year=2026, end_year=2026)
    assert len(docs) == 18


def test_discover_2025_count():
    docs = IAClient().discover_documents(start_year=2025, end_year=2025)
    assert len(docs) == 42


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
