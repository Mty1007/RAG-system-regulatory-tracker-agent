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
the 3 GitHub issues in order (PCPD → IA → SFC), one small PR per source.

## Workflow

Each source is tracked as its own GitHub issue with the spec and acceptance
criteria already in the issue body. For each one:

1. **Comment your plan on the issue first** — 3-5 lines: what you're
   changing, any open questions. Wait for a comment back before writing
   any code.
2. Once aligned, implement it — use IBM Bob, hand it `docs/SPEC.md` and the
   issue, and have it check its own output against the acceptance criteria
   before you open a PR.
3. Open a PR that references the issue (`Closes #<n>`) so it closes
   automatically on merge.
4. CI (lint, tests, commit-lint, regression-test-check) must be green
   before requesting review — no exceptions.

No daily status updates needed — the issue comment before starting, and
the PR when it's ready, are the only two check-ins.
