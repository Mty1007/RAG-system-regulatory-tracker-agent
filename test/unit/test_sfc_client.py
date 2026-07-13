from pathlib import Path

import pytest

from core.sfc_client import SFCClient

FIXTURE = Path(__file__).parent / "fixtures" / "sfc_codes_sample.html"


@pytest.mark.skip(reason="PR 3: remove this skip once SFCClient.parse_listing_page is implemented")
def test_popup_rows_are_excluded():
    """The fixture has 2 real rows (data-code-guideline-id) and 1 popup
    'previous versions' row without that attribute — a naive 'every <tr>
    with a .pdf link' parser would return 3; the correct parser returns 2.
    """
    html = FIXTURE.read_text(encoding="utf-8")
    docs = SFCClient.parse_listing_page(html, base_url="https://www.sfc.hk", document_type="Code")

    assert len(docs) == 2
    titles = {d["title"] for d in docs}
    assert "Code of Conduct for Persons Licensed by or Registered with the Securities and Futures Commission" in titles
    assert "Code of Conduct for Persons Providing Credit Rating Services" in titles
    # Neither real title should be the popup's date-range/link text
    assert "16 Nov 2022" not in titles
