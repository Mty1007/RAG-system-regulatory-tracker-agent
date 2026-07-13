# Regulatory Tracker Agent

Ingestion pipeline for Hong Kong financial regulator documents (SFC, IA,
PCPD). See [`docs/SPEC.md`](docs/SPEC.md) for the task spec and
[`CLAUDE.md`](CLAUDE.md) for coding conventions.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
git config core.hooksPath .githooks
```

## Run

```bash
uvicorn api.main:app --reload --port 8000
```

## Test

```bash
python -m pytest test/unit/ -v
```

## Status

Scaffold only — `core/{sfc,ia,pcpd}_client.py` are stubs. Work through
`docs/SPEC.md` in order: PCPD → IA → SFC, one small PR per source.
