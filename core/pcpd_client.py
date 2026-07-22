from __future__ import annotations

import copy
from typing import Any

from core.doc_id import make_doc_id

_KNOWN_DOCUMENTS: list[dict[str, str]] = [
    {
        "doc_id": make_doc_id("PCPD", "https://www.pcpd.org.hk/english/files/pdpo.pdf"),
        "source": "PCPD",
        "title": "PDPO Full Ordinance",
        "download_url": "https://www.pcpd.org.hk/english/files/pdpo.pdf",
        "source_url": "https://www.pcpd.org.hk/english/data_privacy_law/ordinance_at_a_Glance/overview.html",
        "document_type": "Ordinance",
        "issue_date": "",
    },
    {
        "doc_id": make_doc_id(
            "PCPD",
            "https://www.pcpd.org.hk/english/education_training/individuals/public_seminars/files/PDPO_eng_2025.pdf",
        ),
        "source": "PCPD",
        "title": "Six Data Protection Principles — Overview",
        "download_url": "https://www.pcpd.org.hk/english/education_training/individuals/public_seminars/files/PDPO_eng_2025.pdf",
        "source_url": "https://www.pcpd.org.hk/english/data_privacy_law/data_protection_principles/overview.html",
        "document_type": "Guidance",
        "issue_date": "",
    },
    {
        "doc_id": make_doc_id(
            "PCPD",
            "https://www.pcpd.org.hk/english/resources_centre/publications/files/guidance_datasecurity_e.pdf",
        ),
        "source": "PCPD",
        "title": "Data Security Measures Guidance",
        "download_url": "https://www.pcpd.org.hk/english/resources_centre/publications/files/guidance_datasecurity_e.pdf",
        "source_url": "https://www.pcpd.org.hk/english/resources_centre/publications/guidance/guidance.html",
        "document_type": "Guidance",
        "issue_date": "",
    },
    {
        "doc_id": make_doc_id(
            "PCPD",
            "https://www.pcpd.org.hk/english/resources_centre/publications/files/guidance_note_dbn_e.pdf",
        ),
        "source": "PCPD",
        "title": "Data Breach Handling Guidance Note",
        "download_url": "https://www.pcpd.org.hk/english/resources_centre/publications/files/guidance_note_dbn_e.pdf",
        "source_url": "https://www.pcpd.org.hk/english/resources_centre/publications/guidance/guidance.html",
        "document_type": "Guidance",
        "issue_date": "",
    },
]


class PCPDClient:
    """Returns the fixed set of PCPD/DPO compliance reference PDFs.

    See docs/SPEC.md "PR 1 — PCPD" for the exact documents and normalized
    record shape each entry must produce.
    """

    def discover_documents(self) -> list[dict[str, Any]]:
        return [copy.copy(doc) for doc in _KNOWN_DOCUMENTS]
