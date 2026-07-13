from __future__ import annotations

from fastapi import APIRouter, Depends

from api.dependencies import get_document_store, get_regulator_client
from api.schemas import (
    BulkIngestRequest,
    BulkIngestResponse,
    DiscoveredDocument,
    DiscoverRequest,
    DiscoverResponse,
)
from store.document_store import DocumentStore

router = APIRouter()


def _discover(req: DiscoverRequest) -> list[dict]:
    client = get_regulator_client(req.source)
    if req.source == "IA":
        return client.discover_documents(start_year=req.start_year, end_year=req.end_year)
    return client.discover_documents()


@router.post("/discover", response_model=DiscoverResponse)
def discover(req: DiscoverRequest):
    docs = _discover(req)
    return DiscoverResponse(
        total_docs=len(docs),
        documents=[DiscoveredDocument(**d) for d in docs],
    )


@router.post("/bulk", response_model=BulkIngestResponse)
def bulk_ingest(
    req: BulkIngestRequest,
    store: DocumentStore = Depends(get_document_store),
):
    docs = _discover(req)
    ingested = 0
    skipped = 0
    for doc in docs:
        if store.get_document(doc["doc_id"]):
            skipped += 1
            continue
        store.insert_document(doc)
        ingested += 1
    return BulkIngestResponse(ingested=ingested, skipped=skipped)
