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
