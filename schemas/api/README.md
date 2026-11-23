# API schemas

* Request/response models: `backend/app/api/schemas.py` (pydantic) - the source of truth for the OpenAPI document.
* `openapi.json` is **generated** by `make api-openapi` (`scripts/export_openapi.py`), which needs FastAPI installed (`make setup`).
  `python scripts/export_openapi.py --check` detects drift between the code and the committed file.
* Human-readable reference with every field, status code and example: `docs/API.md`.
