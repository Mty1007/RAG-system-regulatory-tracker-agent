from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

Source = Literal["SFC", "IA", "PCPD"]


class DiscoverRequest(BaseModel):
    source: Source
    start_year: Optional[int] = Field(default=None, ge=2000, le=2100)
    end_year: Optional[int] = Field(default=None, ge=2000, le=2100)

    @model_validator(mode="after")
    def _require_years_for_ia(self) -> "DiscoverRequest":
        if self.source == "IA" and (self.start_year is None or self.end_year is None):
            raise ValueError("start_year and end_year are required for source=IA")
        return self


class DiscoveredDocument(BaseModel):
    doc_id: str
    title: str
    source: Source
    issue_date: str = ""
    document_type: str = ""


class DiscoverResponse(BaseModel):
    total_docs: int
    documents: list[DiscoveredDocument]


class BulkIngestRequest(DiscoverRequest):
    pass


class BulkIngestResponse(BaseModel):
    ingested: int
    skipped: int


class DocumentResponse(BaseModel):
    doc_id: str
    source: Source
    title: str
    download_url: str
    source_url: str
    document_type: str = ""
    issue_date: str = ""
    created_at: Optional[str] = None
