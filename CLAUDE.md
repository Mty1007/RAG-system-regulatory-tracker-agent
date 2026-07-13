# Project: Regulatory Tracker Agent

## Overview

Ingestion pipeline that discovers and downloads regulatory documents (PDFs)
from Hong Kong financial regulators — SFC, IA, and PCPD to start — and
persists them to a document store with a normalized schema, ready for a
later OCR/extraction step. This is the ingestion half only; extraction is
out of scope for this project.

See `docs/SPEC.md` for the full task breakdown and acceptance criteria.

## Architecture

- **Backend:** FastAPI (Python 3.11+), flat layout (no `backend/` nesting):
  - `core/` — one client module per regulator source, each exposing
    `discover_documents(...)` and returning a normalized record shape.
  - `api/` — FastAPI app: `schemas.py` (request/response models),
    `services/ingestion.py` (discovery + persist logic), `routers/ingest.py`
    (endpoints), `dependencies.py` (DI factories).
  - `store/` — document persistence. Defaults to an in-memory store so you
    can run everything with zero external setup; swap in a real backing
    store later without changing the `core/`/`api/` contract.
- **No frontend, no LLM/OCR extraction, no auth** in this project — those
  are deliberately out of scope.

## Coding Discipline

### Assumptions & Ambiguity
- State assumptions explicitly before implementing. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- Push back if a simpler approach exists.

### Simplicity
- No features, abstractions, or error handling beyond what was asked.
- If code could be half the length, rewrite it.
- No speculative "flexibility" or "configurability."

### Surgical Changes
- Touch only what the request requires. Don't improve adjacent code.
- Match existing style, even if you'd do it differently.
- Remove only orphans YOUR changes created. Mention (don't delete) pre-existing dead code.

### Verifiable Goals
- Transform tasks into success criteria before implementing.
- "Fix the bug" → write a reproducing test, then make it pass.
- For multi-step work, state a plan with verification checkpoints.

## Development

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn api.main:app --reload --port 8000
```

## Testing

```bash
python -m pytest test/unit/ -v --tb=short
```

## Git Conventions

Commit messages MUST follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <description>
```

| Type | When to use |
|---|---|
| `feat` | New feature or capability |
| `fix` | Bug fix (MUST include regression test) |
| `refactor` | Restructure code, no behavior change |
| `test` | Add or update tests only |
| `docs` | Documentation only |
| `build` | Dependencies, build config |
| `chore` | Cleanup, gitignore, misc maintenance |

Rules:
- Description: imperative mood, lowercase, no period
- Enforced by git hook (`.githooks/commit-msg`) and CI (`commit-lint` job)

After cloning, activate the hook: `git config core.hooksPath .githooks`

## Workflow

Work in 3 small PRs, in order — see `docs/SPEC.md` for full detail:
1. PCPD client + schema/routing plumbing (foundation)
2. IA client
3. SFC client (has one real parsing trap — budget more time)

Before starting each PR, post a short plan (what you're changing, any open
questions) so direction issues get caught early. Each PR should be small,
self-contained, and pass CI (lint, tests, commit-lint, regression-test-check)
before requesting review.
