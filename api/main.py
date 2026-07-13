from __future__ import annotations

from fastapi import FastAPI

from api.dependencies import init_services
from api.routers import ingest

app = FastAPI(title="Regulatory Tracker Agent")
app.include_router(ingest.router, prefix="/ingest", tags=["ingest"])


@app.on_event("startup")
def _startup() -> None:
    init_services()
