"""The common experiment specification shared by every approach adapter."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

from ..netem.conditions import NetworkConditions

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TOPOLOGY = ROOT / "config" / "topology_ro.yaml"

ENVIRONMENTS = ("emulated", "loopback", "live")
HANDOFF_MODES = ("bbm", "mbb")          # break-before-make / make-before-break


@dataclass
class ExperimentSpec:
    approach: str
    environment: str = "emulated"
    scenario_id: str = ""
    run_id: str = "run-1"
    duration_s: float = 10.0
    rate_pps: float = 100.0
    payload: int = 64
    initial_iface: str = "eth-a"
    moves: tuple = ((4.0, "eth-b"), (7.0, "eth-a"))      # (seconds after t0, interface the mobile node moves to)
    handoff_mode: str = "bbm"
    old_link_hold_ms: float = 100.0                      # MBB: how long the old link stays up after recovery
    detect_delay_ms: float = 20.0                        # movement detection, identical for every approach
    probe_start_s: float = 0.5
    topology: Path = DEFAULT_TOPOLOGY
    # approach parameters
    lifetime_s: int = 30                                 # Mobile IP registration lifetime
    ro_lifetime_s: int = 40                              # route-optimisation binding lifetime
    tcp_detect: str = "event"                            # "event" | "timeout"
    tcp_detect_timeout_s: float = 0.5
    link_semantics: str = "both-ends"                    # "both-ends" | "kernel-veth" (what the live testbed does); see simnet.SimLink
    variant: str = ""                                    # free label to tell scenario variants apart in the matrix
    # Stage 7 (all defaults reproduce the Stage 5/6 behaviour exactly)
    seed: int = 0                                        # recorded with the run; the random streams of `conditions` use conditions.seed
    conditions: NetworkConditions = field(default_factory=NetworkConditions)   # latency / loss / Home Agent distance / registration

    def __post_init__(self):
        if not self.scenario_id:
            self.scenario_id = f"{self.approach}-{self.environment}" + (f"-{self.variant}" if self.variant else "")

    def to_json(self) -> dict:
        d = asdict(self)
        try:                                              # portable: never leak the absolute path of the machine that produced the run
            d["topology"] = str(Path(self.topology).resolve().relative_to(ROOT))
        except ValueError:
            d["topology"] = str(self.topology)
        d["moves"] = [list(m) for m in self.moves]
        d["conditions"] = self.conditions.to_json()
        return d

    @classmethod
    def from_json(cls, d: dict) -> "ExperimentSpec":
        """Inverse of to_json (used to re-run an experiment from its saved experiment.json). Fields missing from files written
        by Stage 5/6 get their defaults; unknown fields are an error rather than being ignored."""
        d = dict(d)
        names = {f.name for f in fields(cls)}
        extra = set(d) - names
        if extra:
            raise ValueError(f"unknown experiment spec field(s) {sorted(extra)}")
        if "moves" in d:
            d["moves"] = tuple((float(t), str(i)) for t, i in d["moves"])
        if "topology" in d:
            t = Path(d["topology"])
            d["topology"] = t if t.is_absolute() else ROOT / t
        d["conditions"] = NetworkConditions.from_json(d.get("conditions"))
        return cls(**d)
