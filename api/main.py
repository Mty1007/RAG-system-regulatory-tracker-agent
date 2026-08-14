from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from api.dependencies import init_services
from api.routers import ingest
from api.routers import chat

# ── load .env before anything else ────────────────────────────────────────────
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

app = FastAPI(title="Regulatory Tracker Agent")
app.include_router(ingest.router, prefix="/ingest", tags=["ingest"])
app.include_router(chat.router, prefix="/chat", tags=["chat"])


@app.on_event("startup")
def _startup() -> None:
    init_services()
