from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from api.dependencies import init_services
from api.routers import agents, chat, ingest
from api.routers import eval as eval_router

# ── load .env before anything else ────────────────────────────────────────────
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ARG001
    init_services()
    yield


app = FastAPI(title="Regulatory Tracker Agent", lifespan=_lifespan)

# ── CORS ───────────────────────────────────────────────────────────────────────
# IBM watsonx Orchestrate calls this API from IBM Cloud.
# Allowed origins are controlled by the ORCHESTRATE_ALLOWED_ORIGINS env var
# (comma-separated URLs).  Falls back to a strict localhost-only default so
# local dev is safe without any extra configuration.
_raw_origins = os.environ.get(
    "ORCHESTRATE_ALLOWED_ORIGINS",
    "http://127.0.0.1:8000,http://localhost:8000",
)
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ── optional inbound IBM Cloud IAM key check ───────────────────────────────────
# When ORCHESTRATE_API_KEY is set, every inbound request must carry it as
# "Authorization: Bearer <key>".  Requests from local dev / tests that omit
# the header are passed through when the env var is not configured.
_ORCHESTRATE_API_KEY = os.environ.get("ORCHESTRATE_API_KEY", "")


@app.middleware("http")
async def _iam_key_guard(request: Request, call_next):
    if _ORCHESTRATE_API_KEY:
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if token != _ORCHESTRATE_API_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized")
    return await call_next(request)


app.include_router(ingest.router,      prefix="/ingest", tags=["ingest"])
app.include_router(chat.router,        prefix="/chat",   tags=["chat"])
app.include_router(agents.router,      prefix="/agent",  tags=["agent"])
app.include_router(eval_router.router, prefix="/eval",   tags=["eval"])
