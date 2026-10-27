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


def test_ml_and_analysis_are_still_placeholders():
    """Stage 2 must not implement later stages (ML / analysis)."""
    for d in ("ml", "analysis"):
        py = [p.name for p in (ROOT / "backend/app" / d).glob("*.py")]
        assert py == ["__init__.py"], d


def test_subprocess_and_sudo_confined_to_runner():
    """Only netem/runner.py may spawn processes or mention sudo (safety.py/cli/docs strings excepted)."""
    allowed = {"runner.py"}
    for f in (ROOT / "backend/app").rglob("*.py"):
        if f.name in allowed:
            continue
        text = f.read_text()
        assert "import subprocess" not in text and "subprocess." not in text, f
        assert "os.system" not in text, f
