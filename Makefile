VENV ?= .venv
PY   := $(VENV)/bin/python

.PHONY: setup check test run-api frontend-install frontend-dev lint
setup:            ## create venv + install Python deps (as normal user)
	./scripts/setup.sh
check:            ## read-only environment diagnostics
	./scripts/check_env.sh
test:             ## run backend tests
	$(PY) -m pytest
run-api:          ## start FastAPI on 127.0.0.1:8000 (unprivileged)
	cd backend && ../$(PY) -m uvicorn app.main:app --reload
frontend-install:
	cd frontend && npm install
frontend-dev:
	cd frontend && npm run dev
frontend-test:    ## vitest: unit, SSR view and (jsdom) interaction tests
	cd frontend && npm test
frontend-build:   ## production build (vite)
	cd frontend && npm run build
frontend-verify:  ## no-network substitute: esbuild bundle + SSR/logic tests without vitest (see docs/FRONTEND.md)
	cd frontend && npm run verify:bundle && npm run verify:sandbox
frontend-integration: ## dashboard API layer against the service layer (stdlib adapter); needs python deps + esbuild
	sh frontend/scripts/run-integration.sh
frontend-fixtures: ## re-capture the real API responses used as test fixtures
	PYTHONPATH=backend $(PY) scripts/capture_frontend_fixtures.py
lint:
	$(PY) -m ruff check backend

.PHONY: topo-validate topo-plan topo-create topo-status topo-destroy test-live
topo-validate:    ## validate config/topology.yaml
	./scripts/topology.sh validate
topo-plan:        ## print commands (no privileges)
	./scripts/topology.sh plan
topo-create:      ## build the topology (uses sudo -n per command)
	./scripts/topology.sh create
topo-status:
	./scripts/topology.sh status
topo-destroy:     ## remove ONLY this project's namespaces
	./scripts/topology.sh destroy
test-live:        ## live topology tests (needs ip/tc/ping + sudo)
	sudo -v && NETSIM_RUN_PRIVILEGED=1 PYTHONPATH=backend $(PY) -m pytest backend/tests/test_netem_integration.py

.PHONY: traffic-examples analyze-example
traffic-examples: ## regenerate examples/results (loopback, no root)
	$(PY) scripts/generate_example_results.py
analyze-example:
	PYTHONPATH=backend $(PY) -m app.traffic analyze examples/results/example-handoff-blackout/example-1 --no-write

.PHONY: mip-sim mip-examples test-live-mip
mip-sim:          ## emulated Mobile IP handoff (no root)
	PYTHONPATH=backend $(PY) -m app.mobileip sim --out-dir data/results
mip-examples:     ## regenerate examples/mobileip
	$(PY) scripts/generate_mobileip_examples.py
test-live-mip:    ## live kernel-tunnel test (Linux, sudo)
	sudo -v && NETSIM_RUN_PRIVILEGED=1 PYTHONPATH=backend $(PY) -m pytest backend/tests/test_mip_live.py

.PHONY: compare compare-list test-compare
compare:          ## run the standard comparison and write the report
	PYTHONPATH=backend $(PY) -m app.compare matrix --out-dir data/results/comparison
compare-list:
	PYTHONPATH=backend $(PY) -m app.compare list

.PHONY: test-report
test-report:      ## run every test module and regenerate docs/TEST_REPORT.md
	$(PY) scripts/run_all_tests.py

.PHONY: ml-features ml-generate ml-pipeline ml-test
ml-features:      ## Stage 6: feature schema, availability, unavailable features
	PYTHONPATH=backend $(PY) -m app.ml features
ml-generate:      ## Stage 6: run 120 controlled emulator experiments into data/ml/raw (~8 min, resumable)
	PYTHONPATH=backend $(PY) -m app.ml generate --raw-dir data/ml/raw --seeds 3 --duration 40
ml-pipeline:      ## Stage 6: dataset -> split -> train -> evaluate -> model bundle, results, plots
	PYTHONPATH=backend $(PY) -m app.ml pipeline --raw-dir data/ml/raw --out-dir data/ml/stage6
ml-test:          ## Stage 6 tests (~1 min)
	cd backend && PYTHONPATH=. $(PY) -m unittest tests.test_ml_features tests.test_ml_labels tests.test_ml_evaluate tests.test_ml_splits tests.test_ml_pipeline

.PHONY: exp-list exp-plan exp-example exp-standard exp-analyze exp-plots exp-reproduce exp-test
exp-list:         ## Stage 7: approaches / configurations / where they can run (JSON)
	PYTHONPATH=backend $(PY) -m app.compare list --json
exp-plan:         ## Stage 7: design of the example campaign (no run)
	PYTHONPATH=backend $(PY) -m app.compare plan --config config/experiments/stage7_example.yaml
exp-example:      ## Stage 7: run/resume the example campaign (372 runs, ~6-8 min), analyse, plot, with Stage 6 predictions
	PYTHONPATH=backend $(PY) -m app.compare campaign --config config/experiments/stage7_example.yaml --out-dir data/experiments/stage7-example --predict-model examples/ml/stage6/model
exp-standard:     ## Stage 7: the full 20-replicate campaign (1,680 runs, ~30 min, ~330 MB raw data)
	PYTHONPATH=backend $(PY) -m app.compare campaign --config config/experiments/stage7_standard.yaml --out-dir data/experiments/stage7-standard
exp-analyze:      ## Stage 7: recompute statistics/exports/report from SAVED results (DIR=...)
	PYTHONPATH=backend $(PY) -m app.compare analyze $(DIR)
exp-plots:        ## Stage 7: charts from saved analysis (DIR=...)
	PYTHONPATH=backend $(PY) -m app.compare plots $(DIR)
exp-reproduce:    ## Stage 7: re-run a sample of saved runs and check they are byte-identical (DIR=...)
	PYTHONPATH=backend $(PY) -m app.compare reproduce $(DIR) --sample 10
exp-test:         ## Stage 7 tests (~3 min)
	cd backend && PYTHONPATH=. $(PY) -m unittest tests.test_exp_stats tests.test_exp_conditions tests.test_exp_campaign tests.test_exp_export tests.test_exp_predictions

.PHONY: api-test api-openapi api-smoke
api-test:         ## Stage 8 API tests (service layer ~1 min; HTTP tests need fastapi+httpx)
	cd backend && PYTHONPATH=. $(PY) -m unittest tests.test_api_validation tests.test_api_store tests.test_api_jobs tests.test_api_results tests.test_api_http
api-openapi:      ## regenerate schemas/api/openapi.json from the code (needs fastapi)
	$(PY) scripts/export_openapi.py
api-smoke:        ## start the job manager without HTTP, run one real experiment through the worker, print the summary
	PYTHONPATH=backend NETSIM_API_DATA_DIR=$${TMPDIR:-/tmp}/netsim-api-smoke $(PY) scripts/api_smoke.py
.PHONY: api-examples
api-examples:     ## regenerate examples/api/stage8 from real runs (add --check via: $(PY) scripts/generate_api_examples.py --check)
	PYTHONPATH=backend $(PY) scripts/generate_api_examples.py
