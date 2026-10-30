"""API/job settings: a plain dataclass read from NETSIM_API_* environment variables (no pydantic, so it is testable anywhere).

Relative paths are resolved against the project root (not the current directory), so `make run-api` (cwd=backend) and the tests
see the same data directory."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import ConfigurationError

ROOT = Path(__file__).resolve().parents[3]
EXPOSED_ENVIRONMENTS = ("emulated", "loopback")      # 'live' (privileged network namespaces) is NEVER exposed through the API


def _read_dotenv(*paths: Path) -> dict:
    """KEY=VALUE lines (no quoting rules beyond stripping matching quotes); the first file that defines a key wins."""
    out: dict = {}
    for p in paths:
        try:
            lines = Path(p).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out.setdefault(k.strip(), v)
    return out


def _path(v, root=ROOT) -> Path:
    p = Path(v).expanduser()
    return p if p.is_absolute() else (root / p)


@dataclass(frozen=True)
class ServiceSettings:
    data_dir: Path = ROOT / "data" / "api"
    max_concurrent_jobs: int = 2
    max_queue_length: int = 20
    max_runtime_s: float = 300.0          # wall-clock limit per job; exceeded -> worker killed, job 'failed' (code 'timeout')
    cancel_grace_s: float = 3.0           # SIGTERM first, SIGKILL after this many seconds
    tick_s: float = 0.1                   # scheduler poll interval
    retention_days: float = 30.0          # finished experiments older than this are deleted (0 = keep forever)
    sweep_interval_s: float = 3600.0
    max_duration_s: float = 120.0         # API limits are tighter than the runner's (3600 s / 5000 pps): see docs/API.md
    max_rate_pps: float = 1000.0
    max_probes: int = 120_000             # duration_s * rate_pps
    max_moves: int = 20
    max_output_mb: int = 512              # RLIMIT_FSIZE of the worker
    allowed_environments: tuple = EXPOSED_ENVIRONMENTS
    model_dirs: dict = field(default_factory=lambda: {"stage6": ROOT / "examples" / "ml" / "stage6" / "model"})
    scenarios_file: Path = ROOT / "config" / "api" / "scenarios.yaml"
    api_token: str | None = None
    allow_root: bool = False
    page_size_max: int = 500

    @property
    def db_path(self) -> Path:
        return self.data_dir / "experiments.sqlite3"

    @property
    def experiments_dir(self) -> Path:
        return self.data_dir / "experiments"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "server.lock"

    def with_(self, **kw) -> "ServiceSettings":
        return replace(self, **kw)

    def validate(self) -> "ServiceSettings":
        bad = []
        if not (1 <= self.max_concurrent_jobs <= 64):
            bad.append("max_concurrent_jobs must be in [1, 64]")
        if not (1 <= self.max_queue_length <= 10_000):
            bad.append("max_queue_length must be in [1, 10000]")
        for n in ("max_runtime_s", "cancel_grace_s", "tick_s", "max_duration_s", "max_rate_pps"):
            if not getattr(self, n) > 0:
                bad.append(f"{n} must be > 0")
        if self.retention_days < 0:
            bad.append("retention_days must be >= 0")
        if self.max_probes < 1 or self.max_moves < 1 or self.max_output_mb < 1:
            bad.append("max_probes, max_moves and max_output_mb must be >= 1")
        unknown = set(self.allowed_environments) - {"emulated", "loopback"}
        if unknown:
            bad.append(f"allowed_environments may only contain {list(EXPOSED_ENVIRONMENTS)} (privileged 'live' runs are never exposed), got {sorted(unknown)}")
        if bad:
            raise ConfigurationError("invalid API settings: " + "; ".join(bad))
        return self

    @classmethod
    def from_env(cls, env=None, root: Path = ROOT) -> "ServiceSettings":
        if env is None:                                  # real process environment, falling back to .env files (cwd, then project root)
            env = {**_read_dotenv(Path.cwd() / ".env", root / ".env"), **os.environ}
        d = cls()
        kw: dict = {}

        def num(name, key, conv):
            raw = env.get(name)
            if raw is None or raw == "":
                return
            try:
                kw[key] = conv(raw)
            except ValueError:
                raise ConfigurationError(f"{name}={raw!r} is not a valid {conv.__name__}") from None

        if env.get("NETSIM_API_DATA_DIR"):
            kw["data_dir"] = _path(env["NETSIM_API_DATA_DIR"], root)
        elif root != ROOT:
            kw["data_dir"] = root / "data" / "api"
        for name, key, conv in (("NETSIM_API_MAX_CONCURRENT_JOBS", "max_concurrent_jobs", int), ("NETSIM_API_MAX_QUEUE", "max_queue_length", int),
                                ("NETSIM_API_MAX_RUNTIME_S", "max_runtime_s", float), ("NETSIM_API_CANCEL_GRACE_S", "cancel_grace_s", float),
                                ("NETSIM_API_RETENTION_DAYS", "retention_days", float), ("NETSIM_API_MAX_DURATION_S", "max_duration_s", float),
                                ("NETSIM_API_MAX_RATE_PPS", "max_rate_pps", float), ("NETSIM_API_MAX_PROBES", "max_probes", int),
                                ("NETSIM_API_MAX_OUTPUT_MB", "max_output_mb", int)):
            num(name, key, conv)
        if env.get("NETSIM_API_ENVIRONMENTS"):
            kw["allowed_environments"] = tuple(x.strip() for x in env["NETSIM_API_ENVIRONMENTS"].split(",") if x.strip())
        if env.get("NETSIM_API_MODEL_DIRS"):        # "id=path,id2=path2"
            dirs = {}
            for part in env["NETSIM_API_MODEL_DIRS"].split(","):
                if part.strip():
                    k, sep, v = part.partition("=")
                    if not sep or not k.strip() or not v.strip():
                        raise ConfigurationError(f"NETSIM_API_MODEL_DIRS entry {part!r} must look like id=path")
                    dirs[k.strip()] = _path(v.strip(), root)
            kw["model_dirs"] = dirs
        if env.get("NETSIM_API_SCENARIOS_FILE"):
            kw["scenarios_file"] = _path(env["NETSIM_API_SCENARIOS_FILE"], root)
        if env.get("NETSIM_API_TOKEN"):
            kw["api_token"] = env["NETSIM_API_TOKEN"]
        if env.get("NETSIM_API_ALLOW_ROOT", "").lower() in ("1", "true", "yes"):
            kw["allow_root"] = True
        return replace(d, **kw).validate()
