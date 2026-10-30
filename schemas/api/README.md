# API schemas

* Request/response models: `backend/app/api/schemas.py` (pydantic) — the source of truth for the OpenAPI document.
* `openapi.json` is **generated** by `make api-openapi` (`scripts/export_openapi.py`) and needs FastAPI installed. It is **not shipped in the
  Stage 8 archive** because FastAPI was not installed where the archive was built; a hand-written copy would only be a claim, not a generated fact.
  Run `make api-openapi` once after `make setup`; `python scripts/export_openapi.py --check` then detects drift.
* Human-readable reference with every field, status code and example: `docs/API.md`.
