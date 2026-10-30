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
    """Only the reviewed modules below may spawn processes: netem/runner.py (privileged namespace commands, Stage 2) and, since Stage 8,
    services/process_launcher.py (the fixed-argv experiment worker). Everything else must not import subprocess or call os.system."""
    allowed = {"netem/runner.py", "compare/runner.py", "services/process_launcher.py"}   # compare/runner.py spawns nothing; it is listed only because the
    for f in (ROOT / "backend/app").rglob("*.py"):                                        # pre-Stage-8 test matched on the file NAME runner.py
        rel = f.relative_to(ROOT / "backend/app").as_posix()
        text = f.read_text()
        assert "os.system" not in text, f
        if rel in allowed:
            continue
        assert "import subprocess" not in text and "subprocess." not in text, f


def test_api_launcher_is_fixed_argv_and_never_privileged():
    """Stage 8: the API can start exactly one kind of process, without a shell, without sudo, and nothing else in api/services spawns or forks."""
    app = ROOT / "backend/app"
    launcher = (app / "services/process_launcher.py").read_text()
    assert "shell=False" in launcher and "shell=True" not in launcher
    assert "start_new_session=True" in launcher
    for f in list((app / "services").rglob("*.py")) + list((app / "api").rglob("*.py")):
        t = f.read_text()
        assert '"sudo"' not in t and "'sudo'" not in t, f                                  # never an argv element
        assert "shell=True" not in t, f
        assert "os.fork" not in t and "os.exec" not in t and "os.popen" not in t, f
        if f.name != "process_launcher.py":
            assert "Popen" not in t and "multiprocessing" not in t, f
