"""Does this checkout work? `make test` runs these.

They check the setup, not your protocol: every scenario loads, stop and wait
runs on each one in ns-3 and delivers, the same seed gives the same results,
and protocol.py, as you have it now, loads and runs without crashing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from transportlab import scenario as scenario_mod  # noqa: E402
from transportlab.orchestrator import run_trial  # noqa: E402

SCENARIOS = ("leo", "datacenter", "sensor")
BASELINE = str(REPO / "example" / "baseline_stopwait")

try:
    from transportlab import _tlsim  # noqa: F401
    BUILT = True
except ImportError:
    BUILT = False
needs_ns3 = pytest.mark.skipif(not BUILT, reason="ns-3 backend not built, run `make setup`")


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_scenario_and_its_basic_workload_load(name):
    s = scenario_mod.load(name)
    assert "basic_workload" in s.workloads()


@needs_ns3
@pytest.mark.parametrize("name", SCENARIOS)
def test_stop_and_wait_runs_and_delivers(tmp_path, name):
    r = run_trial(name, "basic_workload", BASELINE, tmp_path / name, runner_kind="ns3")
    assert r["status"] == "ok", r["events"]
    assert r["metrics"]["reliability"]["delivered"] > 0


@needs_ns3
def test_the_same_seed_gives_the_same_results(tmp_path):
    a = run_trial("sensor", "basic_workload", BASELINE, tmp_path / "a", runner_kind="ns3")
    b = run_trial("sensor", "basic_workload", BASELINE, tmp_path / "b", runner_kind="ns3")
    assert a["metrics"] == b["metrics"]


@needs_ns3
def test_your_protocol_loads_and_runs(tmp_path):
    r = run_trial("leo", "basic_workload", str(REPO / "protocol.py"), tmp_path / "mine", runner_kind="ns3")
    assert r["status"] == "ok", r["events"]
