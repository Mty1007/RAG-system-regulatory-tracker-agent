from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

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
    return client.discover_documents()


@router.post("/discover", response_model=DiscoverResponse)
def discover(req: DiscoverRequest) -> DiscoverResponse:
    try:
        docs = _discover(req)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Discovery error: {exc}") from exc
    return DiscoverResponse(
        total_docs=len(docs),
        documents=[DiscoveredDocument(**d) for d in docs],
    )


@router.post("/bulk", response_model=BulkIngestResponse)
def bulk_ingest(
    req: BulkIngestRequest,
    store: DocumentStore = Depends(get_document_store),
) -> BulkIngestResponse:
    try:
        docs = _discover(req)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Discovery error: {exc}") from exc
    ingested = 0
    skipped = 0
    failed: list[str] = []
    for doc in docs:
        try:
            if store.get_document(doc["doc_id"]):
                skipped += 1
                continue
            store.insert_document(doc)
            ingested += 1
        except Exception as exc:
            # Accumulate failures instead of raising immediately so the caller
            # always receives the counts for docs already processed.
            failed.append(f"{doc.get('doc_id')}: {exc}")

    if failed:
        raise HTTPException(
            status_code=502,
            detail=f"Store errors on {len(failed)} doc(s): {'; '.join(failed[:5])}; "
                   f"ingested={ingested} skipped={skipped}",
        )
    return BulkIngestResponse(ingested=ingested, skipped=skipped)
