"""Step time, the datacenter's per-round metric, on made-up deliveries.

No simulator needed: these build a trace from a card and hand build_results
the deliveries directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from transportlab import WIRE_HEADER_BYTES  # noqa: E402
from transportlab import scenario as scenario_mod  # noqa: E402
from transportlab.metrics import build_results  # noqa: E402
from transportlab.trace import build_trace, trace_hash  # noqa: E402

CARD = {
    "id": "t",
    "seed": 1,
    "flows": [{
        "pattern": "incast", "src": "sender*", "dst": "receiver*", "exclude_self": True,
        "start_s": 1.0, "period_s": 2.0, "rounds": 2, "burst_bytes": 2800, "msg_bytes": 1400,
    }],
}


def _trace(card=CARD):
    s = scenario_mod.load("datacenter")
    return s, build_trace(card, s.hosts, s.claims.time_factor, s.wire_time_divisor)


def _results(s, trace, latency_s, skip=(), card=CARD, duration_s=10.0):
    """Every message arrives latency_s after it was offered, except `skip`."""
    by_dst: dict[str, list] = {}
    for m in trace:
        if m.msg_id not in skip:
            by_dst.setdefault(m.dst, []).append(
                {"msg_id": m.msg_id, "latency_s": latency_s, "t_s": m.t_s + latency_s})
    stats = [{"endpoint": d, "role": "receiver", "counters": {"delivered": len(v)},
              "samples": v} for d, v in by_dst.items()]
    return build_results(scenario=s, card=card, trace=trace, protocol_label="t",
                         endpoint_stats=stats, duration_s=duration_s, status="ok",
                         events=[], trace_fingerprint=trace_hash(trace))


def test_incast_rounds_are_groups():
    _, trace = _trace()
    assert {m.group for m in trace} == {"0:0", "0:1"}
    # 9 servers, 8 peers each, 2 messages per pair
    assert sum(m.group == "0:0" for m in trace) == 144


def test_traces_without_groups_hash_as_before():
    card = {"id": "b", "seed": 1, "flows": [{"pattern": "bulk", "src": "sender0",
            "dst": "receiver1", "total_bytes": 2800, "msg_bytes": 1400}]}
    _, trace = _trace(card)
    assert all("group" not in m.to_dict() for m in trace)


def test_the_ideal_is_delay_plus_the_busiest_link():
    s, trace = _trace()
    steps = _results(s, trace, 0.1)["scenario_specific"]["steps"]
    # Cross-rack paths are 50 ms one way. The busiest links are the racks'
    # uplinks: 3 senders x 6 receivers in other racks x 2 packets, over
    # 150 Mbps.
    bits = 36 * 8 * (1400 + WIRE_HEADER_BYTES)
    ideal_ms = 50.0 + 1000.0 * bits / 150e6
    for r in steps["rounds"]:
        assert abs(r["ideal_ms"] - ideal_ms) < 0.01
        assert r["step_ms"] == 100.0
    assert abs(steps["slowdown"]["mean"] - 100.0 / ideal_ms) < 0.001


def test_an_undelivered_message_holds_its_round_open():
    s, trace = _trace()
    lost = next(m for m in trace if m.group == "0:1")
    steps = _results(s, trace, 0.1, skip={lost.msg_id})["scenario_specific"]["steps"]
    first, second = steps["rounds"]
    assert first["step_ms"] == 100.0 and first["undelivered"] == 0
    assert second["step_ms"] == (10.0 - lost.t_s) * 1000.0
    assert second["undelivered"] == 1


def test_a_slower_link_raises_the_ideal():
    card = dict(CARD, events=[{"t_s": 2.0, "kind": "bw_change", "target": "rack0-spine",
                               "bw_mbps": 50, "duration_s": 5.0}])
    s, trace = _trace(card)
    first, second = _results(s, trace, 0.1, card=card)["scenario_specific"]["steps"]["rounds"]
    assert second["ideal_ms"] > first["ideal_ms"]


def test_a_failed_server_holds_the_ideal_until_it_is_back():
    # Server 4 goes dark just after the second round starts, for half a second.
    card = dict(CARD, events=[
        {"t_s": 3.01, "kind": "blackout", "target": "sender4-rack1", "duration_s": 0.5},
        {"t_s": 3.01, "kind": "blackout", "target": "rack1-receiver4", "duration_s": 0.5},
    ])
    s, trace = _trace(card)
    first, second = _results(s, trace, 0.1, card=card)["scenario_specific"]["steps"]["rounds"]
    # Nothing to or from server 4 can arrive before 3.51 s, half a second after
    # the round started, plus the path's delay.
    assert second["ideal_ms"] > 510.0
    assert first["ideal_ms"] < 100.0


def test_step_slowdown_is_a_requirement_on_datacenter():
    s, trace = _trace()
    checked = _results(s, trace, 0.1)["requirements"]["checked"]
    assert checked["step_slowdown.mean_max"]["met"]
    slow = _results(s, trace, 5.0)["requirements"]["checked"]
    assert not slow["step_slowdown.mean_max"]["met"]


def test_deadline_met_is_counted_per_direction():
    card = {"id": "a", "seed": 1, "flows": [
        {"pattern": "bulk", "src": "sender0", "dst": "receiver1", "total_bytes": 2800,
         "msg_bytes": 1400, "direction": "bulk", "deadline_s": 1.0},
        {"pattern": "bulk", "src": "sender0", "dst": "receiver1", "total_bytes": 400,
         "msg_bytes": 100, "direction": "alarm", "deadline_s": 0.05},
    ]}
    s, trace = _trace(card)
    # everything takes 0.1 s: in time for the bulk messages, late for alarms
    r = _results(s, trace, 0.1, card=card)
    assert r["scenario_specific"]["deadline_met_by_direction"] == {"alarm": 0.0, "bulk": 1.0}
