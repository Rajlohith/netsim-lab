from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

EXPECTED = [
    "README.md", "requirements.txt", "requirements-dev.txt", "pytest.ini", ".gitignore",
    ".env.example", "Makefile",
    "backend/app/main.py", "backend/app/config.py",
    "backend/app/netem/__init__.py", "backend/app/ml/__init__.py", "backend/app/analysis/__init__.py",
    "frontend/package.json", "frontend/src/App.jsx",
    "scripts/setup.sh", "scripts/check_env.sh", "scripts/topology.sh", "config/topology.yaml",
    "backend/app/traffic/sender.py", "backend/app/traffic/receiver.py", "backend/app/traffic/metrics.py",
    "schemas/csv_columns.json", "docs/TRAFFIC_MEASUREMENT.md",
    "backend/app/mobileip/home_agent.py", "backend/app/mobileip/mobile_node.py", "backend/app/mobileip/ipip.py",
    "backend/app/mobileip/mobility_sim.py", "backend/app/mobileip/demo.py", "docs/MOBILE_IP.md",
    "backend/app/compare/runner.py", "backend/app/compare/approaches.py", "backend/app/compare/tcplite.py",
    "backend/app/compare/quic_sim.py", "backend/app/mobileip/ro.py", "config/topology_ro.yaml", "docs/MOBILITY_COMPARISON.md",
    "docs/ARCHITECTURE.md", "docs/PRIVILEGED_OPERATIONS.md",
]


def test_expected_files_exist():
    missing = [p for p in EXPECTED if not (ROOT / p).exists()]
    assert not missing, f"missing: {missing}"


def test_analysis_is_still_a_placeholder_and_ml_is_implemented():
    """Stage 2 asserted that ml/ and analysis/ were both placeholders. Stage 6 implements ml/ (this assertion was changed on
    purpose); analysis/ (a dashboard / plotting layer) is still a placeholder."""
    assert [p.name for p in (ROOT / "backend/app/analysis").glob("*.py")] == ["__init__.py"]
    ml = {p.name for p in (ROOT / "backend/app/ml").glob("*.py")}
    assert {"features.py", "labels.py", "dataset.py", "splits.py", "models.py", "evaluate.py", "bundle.py", "predict.py", "pipeline.py"} <= ml


def test_subprocess_and_sudo_confined_to_runner():
    """Only netem/runner.py may spawn processes or mention sudo (safety.py/cli/docs strings excepted)."""
    allowed = {"runner.py"}
    for f in (ROOT / "backend/app").rglob("*.py"):
        if f.name in allowed:
            continue
        text = f.read_text()
        assert "import subprocess" not in text and "subprocess." not in text, f
        assert "os.system" not in text, f
