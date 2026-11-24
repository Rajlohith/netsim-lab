"""Opt-in TcpLite loss recovery (RFC 5681 fast retransmit + out-of-order buffering).

Default OFF must leave every existing result unchanged (see test_compare_emulated for the byte-for-byte baseline check); switched
ON it must stop a single lost segment from stalling the stream into the retransmission back-off."""
import json
import tempfile
import unittest
from pathlib import Path

from app.compare.experiment import ExperimentSpec
from app.compare.runner import run_experiment
from app.compare.tcplite import TcpLiteStack
from app.netem.conditions import NetworkConditions


def _loss_percent(fast: bool, seed: int, loss: float) -> float:
    old = TcpLiteStack.FAST_RETRANSMIT_DEFAULT
    TcpLiteStack.FAST_RETRANSMIT_DEFAULT = fast
    try:
        with tempfile.TemporaryDirectory() as tmp:
            spec = ExperimentSpec(approach="tcp-reconnect", scenario_id="t", duration_s=12.0,
                                  conditions=NetworkConditions(loss_percent=loss, seed=seed), seed=seed)
            res = run_experiment(spec, Path(tmp))
            assert res.status == "ok", res.reason
            m = json.loads((Path(tmp) / "t" / "run-1" / "comparison_metrics.json").read_text())
            return m["loss"]["percent"]
    finally:
        TcpLiteStack.FAST_RETRANSMIT_DEFAULT = old


class FastRetransmit(unittest.TestCase):
    def test_default_is_off(self):
        self.assertFalse(TcpLiteStack.FAST_RETRANSMIT_DEFAULT)

    def test_on_removes_the_backlog_loss(self):
        off = _loss_percent(False, 11, 5.0)
        on = _loss_percent(True, 11, 5.0)
        self.assertGreater(off, 20.0, "without recovery the model leaves a large undelivered backlog at 5 % link loss")
        self.assertLess(on, 1.0, "with fast retransmit the same seed delivers (almost) every probe")

    def test_no_loss_is_identical(self):
        self.assertEqual(_loss_percent(False, 3, 0.0), _loss_percent(True, 3, 0.0))


if __name__ == "__main__":
    unittest.main()
