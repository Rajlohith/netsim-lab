"""Stage 8: framework-independent business logic behind the HTTP API.

Nothing in this package imports FastAPI or pydantic: the API layer (app/api) only translates HTTP <-> these functions, so every
rule below (validation, duplicate prevention, job lifecycle, cancellation, persistence, exports) is unit-testable without a web
framework. Experiments are executed by the EXISTING modules (app.compare.run_experiment, app.ml.integration.annotate_run); this
package adds orchestration, never measurements.
"""
