# NetSim Lab - every common task as a target.  Run `make help` for the list.
# Everything runs as your normal user. Only the optional "topo-*" / "test-live*" targets use sudo.

VENV ?= .venv
PY   := $(VENV)/bin/python

.DEFAULT_GOAL := help
.PHONY: help
help:             ## list the targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

# ---- install -------------------------------------------------------------------------------------------------------------
.PHONY: setup check frontend-install
setup:            ## create .venv, install Python dependencies, create .env
	bash ./scripts/setup.sh
check:            ## read-only environment diagnostics
	bash ./scripts/check_env.sh
frontend-install: ## npm install (needs Node.js 20.19 or newer)
	cd frontend && npm install

# ---- run -------------------------------------------------------------------------------------------------------------------
.PHONY: stack-start stack-stop stack-status stack-logs run-api frontend-dev frontend-build
stack-start:      ## ONE terminal: start API (127.0.0.1:8000) + dashboard (127.0.0.1:5173) in the background
	bash ./scripts/stack.sh start all
stack-stop:       ## stop both background services
	bash ./scripts/stack.sh stop all
stack-status:     ## which services are running, plus the API health answer
	bash ./scripts/stack.sh status
stack-logs:       ## follow the API log of the background stack (Ctrl+C to leave)
	bash ./scripts/stack.sh logs api
run-api:          ## TWO terminals, terminal 1: API in the foreground with auto-reload (Ctrl+C stops it)
	cd backend && ../$(PY) -m uvicorn app.main:app --reload --host $${NETSIM_API_HOST:-127.0.0.1} --port $${NETSIM_API_PORT:-8000}
frontend-dev:     ## TWO terminals, terminal 2: dashboard dev server in the foreground (Ctrl+C stops it)
	cd frontend && npm run dev -- --host 127.0.0.1 --port $${NETSIM_FRONTEND_PORT:-5173}
frontend-build:   ## production build of the dashboard (frontend/dist)
	cd frontend && npm run build

# ---- tests and checks ------------------------------------------------------------------------------------------------------
.PHONY: test test-report api-test frontend-test frontend-integration lint e2e-browser api-smoke api-openapi
test:             ## backend tests with pytest (a few minutes)
	$(PY) -m pytest
test-report:      ## every backend test module in its own process; writes data/reports/TEST_REPORT.md (about 12 minutes)
	$(PY) scripts/run_all_tests.py
api-test:         ## API tests, including the HTTP layer (about 1 minute)
	cd backend && PYTHONPATH=. ../$(PY) -m unittest tests.test_api_validation tests.test_api_store tests.test_api_jobs tests.test_api_results tests.test_api_http
frontend-test:    ## dashboard unit, view and interaction tests (vitest)
	cd frontend && npm test
frontend-integration: ## dashboard API layer against the service layer (needs Python deps and esbuild)
	sh frontend/scripts/run-integration.sh
lint:             ## ruff check of the backend
	$(PY) -m ruff check backend
e2e-browser:      ## drive the dashboard in real headless Chrome (needs Playwright, Chrome, esbuild)
	python3 scripts/e2e_browser.py --out data/reports/e2e_browser_results.json --screenshots data/reports/screenshots
api-smoke:        ## run one real experiment through the job manager without a web server
	PYTHONPATH=backend NETSIM_API_DATA_DIR=$${TMPDIR:-/tmp}/netsim-api-smoke $(PY) scripts/api_smoke.py
api-openapi:      ## regenerate schemas/api/openapi.json from the code
	$(PY) scripts/export_openapi.py

# ---- experiments from the command line -------------------------------------------------------------------------------------
.PHONY: compare compare-list mip-sim exp-list exp-plan exp-example exp-standard exp-analyze exp-plots exp-reproduce exp-test
compare:          ## run the standard comparison matrix and write the report (data/results/comparison)
	PYTHONPATH=backend $(PY) -m app.compare matrix --out-dir data/results/comparison
compare-list:     ## the approaches and where they can run
	PYTHONPATH=backend $(PY) -m app.compare list
mip-sim:          ## emulated Mobile IP handoff demonstration (no root)
	PYTHONPATH=backend $(PY) -m app.mobileip sim --out-dir data/results
exp-list:         ## approaches / configurations / where they can run (JSON)
	PYTHONPATH=backend $(PY) -m app.compare list --json
exp-plan:         ## design of the example campaign (no run)
	PYTHONPATH=backend $(PY) -m app.compare plan --config config/experiments/example.yaml
exp-example:      ## run or resume the example campaign (372 runs, about 15-20 minutes), analyse and plot it
	PYTHONPATH=backend $(PY) -m app.compare campaign --config config/experiments/example.yaml --out-dir data/experiments/example
exp-standard:     ## the full 20-replicate campaign (about 1,700 runs, about 30 minutes, about 330 MB)
	PYTHONPATH=backend $(PY) -m app.compare campaign --config config/experiments/standard.yaml --out-dir data/experiments/standard
exp-analyze:      ## recompute statistics, exports and the report from SAVED results: make exp-analyze DIR=data/experiments/example
	PYTHONPATH=backend $(PY) -m app.compare analyze $(DIR)
exp-plots:        ## charts from the saved analysis: make exp-plots DIR=...
	PYTHONPATH=backend $(PY) -m app.compare plots $(DIR)
exp-reproduce:    ## re-run a sample of saved runs and check they are byte-identical: make exp-reproduce DIR=...
	PYTHONPATH=backend $(PY) -m app.compare reproduce $(DIR) --sample 10
exp-test:         ## statistics / conditions / campaign / export tests (about 3 minutes)
	cd backend && PYTHONPATH=. ../$(PY) -m unittest tests.test_exp_stats tests.test_exp_conditions tests.test_exp_campaign tests.test_exp_export

# ---- optional: the real network-namespace lab (needs sudo; read docs/PRIVILEGED_OPERATIONS.md first) -----------------------
.PHONY: topo-validate topo-plan topo-create topo-status topo-destroy test-live test-live-mip
topo-validate:    ## validate config/topology.yaml
	bash ./scripts/topology.sh validate
topo-plan:        ## print the commands that would run (no privileges)
	bash ./scripts/topology.sh plan
topo-create:      ## build the topology (uses sudo -n per command)
	bash ./scripts/topology.sh create
topo-status:      ## show the state of the topology
	bash ./scripts/topology.sh status
topo-destroy:     ## remove ONLY this project's namespaces
	bash ./scripts/topology.sh destroy
test-live:        ## live topology tests (needs ip, tc, ping and sudo)
	sudo -v && NETSIM_RUN_PRIVILEGED=1 PYTHONPATH=backend $(PY) -m pytest backend/tests/test_netem_integration.py
test-live-mip:    ## live kernel-tunnel test (Linux, sudo)
	sudo -v && NETSIM_RUN_PRIVILEGED=1 PYTHONPATH=backend $(PY) -m pytest backend/tests/test_mip_live.py
