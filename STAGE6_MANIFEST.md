# STAGE6_MANIFEST

ZIP: `netsim-lab-stage-6.zip` (entire updated project, stages 1-6).

## Validation status

* **full_suite_command**: python3 scripts/run_all_tests.py   (unittest discovery; pytest is not installed in the sandbox)
* **result**: 356 ran: 344 passed, 0 failed, 12 skipped; 2 modules not runnable (test_api, test_config: fastapi/pydantic missing)
* **baseline_before_stage_6**: 291 ran by plain unittest discovery (2 import errors, 12 skipped); status doc baseline 293 ran / 281 passed
* **new_ml_tests**: {'test_ml_features': 10, 'test_ml_labels': 9, 'test_ml_evaluate': 10, 'test_ml_splits': 7, 'test_ml_pipeline': 27, 'all_passed': True}
* **changed_existing_test**: tests/test_project_structure.py: placeholder assertion for ml/ replaced (ml is now implemented; analysis/ remains a placeholder)
* **skipped_unchanged**: 12 skips are environmental (no root/iproute2/ipip/aioquic), same as before Stage 6
* **end_to_end_run**: python -m app.ml generate (120/120 runs ok) -> pipeline (status evaluated); rerun from the shipped dataset reproduced results.json exactly except the timestamp
* **pending_path_verified**: python -m app.ml pipeline --raw-dir examples/comparison -> status pending (5 scenario groups < 6), no model written
* **implemented_but_not_verified**: ['live (real namespace) runs with the ML pipeline', 'FastAPI/dashboard integration', 'QUIC data in the dataset (adapter not runnable here)', 'scikit-learn versions other than 1.8.0']
* **claims_not_made**: no predictive skill on real networks; results are on simulated data; no radio signal strength

## Key Stage 6 files

* `backend/app/ml/` pipeline (features, labels, dataset, splits, models, evaluate, bundle, predict, integration, datagen, plots, cli)
* `backend/tests/test_ml_*.py` tests
* `docs/HANDOFF_PREDICTION.md` design, labelling, results, limitations
* `examples/ml/stage6/` dataset.csv, model bundle, results.json/csv, RESULTS.md, plots

Not included: data/ (raw generated runs, ~100 MB; recreate with `make ml-generate`); virtual environments, node_modules, caches, __pycache__, secrets

365 files are listed with sha256 in `STAGE6_MANIFEST.json` (this manifest itself is not self-hashed).
