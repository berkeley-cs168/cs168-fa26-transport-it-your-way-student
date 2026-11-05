"""Turn raw endpoint counters into results.json.

Four metric keys are fixed forever, because an autograder has to read them
without knowing which scenario ran: throughput_bps, latency_ms, reliability,
overhead. Anything scenario-shaped goes under scenario_specific. tests/ has a
test that fails if the four keys move.
"""

from __future__ import annotations

import csv
import json
import math
from collections import deque
from pathlib import Path

from . import SCHEMA_VERSION, WIRE_HEADER_BYTES
from .api import MAX_DATAGRAM as MAX_DATAGRAM_BYTES
from .scenario import Claims, Scenario
from .trace import Message


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile. Empty input gives 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (p / 100.0)
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _ratio(num: float, den: float) -> float:
    return round(num / den, 6) if den else 0.0


def build_results(
    *,
    scenario: Scenario,
    card: dict,
    trace: list[Message],
    protocol_label: str,
    endpoint_stats: list[dict],
    duration_s: float,
    status: str,
    events: list[dict],
    trace_fingerprint: str,
) -> dict:
    claims: Claims = scenario.claims
    by_id = {m.msg_id: m for m in trace}
    senders = [s for s in endpoint_stats if s.get("role") == "sender"]
    receivers = [s for s in endpoint_stats if s.get("role") == "receiver"]

    def total(field: str, group=endpoint_stats) -> int:
        return sum(int(s.get("counters", {}).get(field, 0)) for s in group)

    offered = len(trace)
    delivered = total("delivered", receivers)
    late = total("late", receivers)
    corrupt = total("corrupt", receivers)
    duplicates = total("duplicates", receivers)
    unknown = total("unknown", receivers)
    app_bytes_delivered = total("app_bytes_delivered", receivers)
    app_bytes_offered = sum(m.size for m in trace)
    wire_bytes = total("wire_bytes_sent")
    datagrams_sent = total("datagrams_sent")

    samples = [s for r in receivers for s in r.get("samples", [])]
    latencies_ms = [s["latency_s"] * 1000.0 for s in samples]

    bytes_by_direction: dict[str, int] = {}
    for s in samples:
        msg = by_id.get(s["msg_id"])
        if msg is not None:
            bytes_by_direction[msg.direction] = bytes_by_direction.get(msg.direction, 0) + msg.size

    span = duration_s if duration_s > 0 else 1.0
    throughput = {"total": round(8.0 * app_bytes_delivered / span, 3)}
    for label, nbytes in sorted(bytes_by_direction.items()):
        throughput[label] = round(8.0 * nbytes / span, 3)

    # `total` divides by the whole trial, so a transfer that finishes early looks
    # slow for a reason that has nothing to do with the protocol. `active` divides
    # by the time between the first offered message and the last delivered one,
    # which is the number to compare against a link rate.
    first_offer = min((m.t_s for m in trace), default=0.0)
    last_delivery = max((s["t_s"] for s in samples), default=first_offer)
    active_span = max(1e-6, last_delivery - first_offer)
    throughput["active"] = round(8.0 * app_bytes_delivered / active_span, 3)

    latency_ms = {
        "p50": round(percentile(latencies_ms, 50), 4),
        "p95": round(percentile(latencies_ms, 95), 4),
        "p99": round(percentile(latencies_ms, 99), 4),
        "max": round(max(latencies_ms), 4) if latencies_ms else 0.0,
        "mean": round(sum(latencies_ms) / len(latencies_ms), 4) if latencies_ms else 0.0,
        "deadline_met_ratio": _ratio(delivered - late, offered),
    }
    reliability = {
        "offered": offered,
        "delivered": delivered,
        "ratio": _ratio(delivered, offered),
        "corrupt": corrupt,
        "duplicates": duplicates,
        "unknown": unknown,
        "late": late,
    }
    overhead = {
        "app_bytes": app_bytes_delivered,
        "app_bytes_offered": app_bytes_offered,
        "wire_bytes": wire_bytes,
        "ratio": _ratio(wire_bytes, app_bytes_delivered),
        "datagrams_sent": datagrams_sent,
        "bytes_per_delivered_msg": _ratio(wire_bytes, delivered),
    }

    steps = _steps(scenario, card, trace, samples, duration_s)

    # Per direction label, the fraction of offered messages delivered within
    # their own deadline (untimed ones count once delivered), so a requirement
    # can hold a few urgent messages to a bar that the bulk would swamp.
    on_time: dict[str, list[int]] = {}
    for m in trace:
        on_time.setdefault(m.direction, [0, 0])[1] += 1
    for smp in samples:
        m = by_id.get(smp["msg_id"])
        if m is not None and (m.deadline_s is None or smp["latency_s"] <= m.deadline_s):
            on_time[m.direction][0] += 1
    deadline_met = {d: _ratio(met, n) for d, (met, n) in sorted(on_time.items())}

    claimed = {
        "throughput_bps": {
            k: round(v * claims.throughput_multiplier, 3) for k, v in throughput.items()
        },
        "latency_ms": {
            k: round(v * claims.latency_multiplier, 4)
            for k, v in latency_ms.items()
            if k != "deadline_met_ratio"
        },
    }
    if steps:
        claimed["step_ms"] = {k: round(v * claims.latency_multiplier, 4)
                              for k, v in steps["step_ms"].items()}

    integrity = _integrity(receivers, app_bytes_delivered, endpoint_stats)
    crashed = [s["endpoint"] for s in endpoint_stats if s.get("crashed")]
    if crashed and status == "ok":
        status = "crash"

    results = {
        "schema_version": SCHEMA_VERSION,
        "scenario": scenario.id,
        "workload": card.get("id"),
        "protocol": protocol_label,
        "seed": int(card.get("seed", 0)),
        "status": status,
        "duration_s": round(duration_s, 4),
        "trace_fingerprint": trace_fingerprint,
        "metrics": {
            "throughput_bps": throughput,
            "latency_ms": latency_ms,
            "reliability": reliability,
            "overhead": overhead,
        },
        "claimed": claimed,
        "claims": claims.to_dict(),
        "requirements": _check_requirements(scenario, throughput, latency_ms, reliability,
                                           overhead, claims, steps, deadline_met),
        "integrity": integrity,
        "crashed_endpoints": crashed,
        "timeseries_csv": "timeseries.csv",
        "events": events + [e for s in endpoint_stats for e in s.get("events", [])],
        "scenario_specific": {
            **({"steps": steps} if steps else {}),
            "deadline_met_by_direction": deadline_met,
            "active_span_s": round(active_span, 4),
            "first_offer_s": round(first_offer, 4),
            "last_delivery_s": round(last_delivery, 4),
            "per_sender": {
                s["endpoint"]: {
                    "datagrams_sent": s["counters"].get("datagrams_sent", 0),
                    "wire_bytes_sent": s["counters"].get("wire_bytes_sent", 0),
                    "offered": s["counters"].get("offered", 0),
                    "unoffered": s.get("unoffered", 0),
                }
                for s in senders
            },
            "per_receiver": {
                s["endpoint"]: {
                    "delivered": s["counters"].get("delivered", 0),
                    "expected": s.get("expected", 0),
                }
                for s in receivers
            },
        },
    }
    return results


def _steps(scenario: Scenario, card: dict, trace: list[Message], samples: list[dict],
           duration_s: float) -> dict:
    """Completion time of each group of messages, such as one incast round.

    A round is done when its last message arrives, so its step time is the
    latency of its slowest message, which p99 is free to ignore. A message that
    never arrived holds the round open until the trial ends.

    Each step is compared with the best the network allows: the longest path's
    one-way delay, plus the time the most loaded link needs to carry the
    round's bytes, headers included. `slowdown` is step over ideal, so one
    bound works for every burst size. The ideal uses each link's bandwidth and
    delay as they are when the round starts, card events included, so a
    slower link raises the ideal rather than the slowdown. A link that goes
    dark during the round (a failed server, say) holds back everything that
    crosses it: nothing can be faster than waiting for it to come back, then
    carrying what crosses it. Loss and other traffic are left out: coping
    with those is the job.
    """
    groups: dict[str, list[Message]] = {}
    for m in trace:
        if m.group is not None:
            groups.setdefault(m.group, []).append(m)
    if not groups:
        return {}

    arrived: dict[int, float] = {}
    for s in samples:
        arrived.setdefault(s["msg_id"], s["t_s"])
    network = _Network(scenario, card)
    paths: dict[tuple[str, str], list[tuple[str, str]]] = {}

    def path(src, dst):
        if (src, dst) not in paths:
            paths[(src, dst)] = _route(network.base, src, dst)
        return paths[(src, dst)]

    rounds = []
    for name in sorted(groups, key=lambda g: tuple(int(x) if x.isdigit() else x
                                                   for x in g.split(":"))):
        msgs = groups[name]
        offered = min(m.t_s for m in msgs)
        done = max(arrived.get(m.msg_id, duration_s) for m in msgs)
        links = network.at(offered)
        load: dict[tuple[str, str], float] = {}
        delay = 0.0
        for m in msgs:
            hops = path(m.src, m.dst)
            delay = max(delay, sum(links[h][1] for h in hops))
            datagrams = max(1, math.ceil(m.size / MAX_DATAGRAM_BYTES))
            bits = 8.0 * (m.size + WIRE_HEADER_BYTES * datagrams)
            for h in hops:
                load[h] = load.get(h, 0.0) + bits
        serialize = max((bits / links[h][0] for h, bits in load.items()), default=0.0)
        ideal = delay + serialize
        for h, bits in load.items():
            back = network.back_up(h, offered, offered + ideal)
            if back is not None:
                through = max(sum(links[x][1] for x in path(m.src, m.dst))
                              for m in msgs if h in path(m.src, m.dst))
                ideal = max(ideal, back - offered + bits / links[h][0] + through)
        step = max(0.0, done - offered)
        rounds.append({
            "group": name,
            "offered_s": round(offered, 6),
            "step_ms": round(step * 1000.0, 4),
            "ideal_ms": round(ideal * 1000.0, 4),
            "slowdown": round(step / ideal, 4) if ideal > 0 else 0.0,
            "undelivered": sum(1 for m in msgs if m.msg_id not in arrived),
        })

    def summary(field):
        values = [r[field] for r in rounds]
        return {"max": max(values), "mean": round(sum(values) / len(values), 4)}

    return {
        "step_ms": summary("step_ms"),
        "slowdown": summary("slowdown"),
        "rounds": rounds,
    }


class _Network:
    """Each link direction's (bits per second, one-way delay in seconds), keyed
    by (from, to), on the scenario's baseline and after each card event."""

    def __init__(self, scenario: Scenario, card: dict):
        from .events import link_timeline, schedule_from_card
        from .topo import normalize_links

        specs = normalize_links(scenario)
        self._ends = {}
        for spec in specs:
            self._ends[(spec.name, 0)] = (spec.a, spec.b)
            self._ends[(spec.name, 1)] = (spec.b, spec.a)
        self.base = {}
        for spec in specs:
            self.base[(spec.a, spec.b)] = self._shape(spec.params1)
            self.base[(spec.b, spec.a)] = self._shape(spec.params2)
        actions = schedule_from_card(card, specs, scenario.card_divisor(card))
        self._changes = link_timeline(actions, specs)

    @staticmethod
    def _shape(params):
        from .topo import UNSHAPED_BPS
        bw = params.get("bw")
        bps = float(bw) * 1e6 if bw else float(UNSHAPED_BPS)
        delay = float(str(params.get("delay", "0ms")).removesuffix("ms")) / 1000.0
        return bps, delay

    def back_up(self, hop: tuple[str, str], t0: float, t1: float) -> float | None:
        """If the link direction is dark at any time in [t0, t1], when it
        comes back. None if it stays up, or never comes back."""
        dark = False
        for change in self._changes:      # in time order
            if self._ends[(change["link"], change["wire"])] != hop:
                continue
            now_dark = change["params"].get("loss", 0.0) >= 100.0
            if change["t_s"] <= t0:
                dark = now_dark
            elif dark and not now_dark:
                return change["t_s"]
            elif not dark and change["t_s"] > t1:
                return None
            else:
                dark = now_dark
        return None

    def at(self, t_s: float) -> dict:
        out = dict(self.base)
        for change in self._changes:      # in time order
            if change["t_s"] > t_s:
                break
            out[self._ends[(change["link"], change["wire"])]] = self._shape(change["params"])
        return out


def _route(links, src: str, dst: str) -> list[tuple[str, str]]:
    """Fewest hops from src to dst, the way the simulator routes."""
    adjacency: dict[str, list[str]] = {}
    for a, b in links:
        adjacency.setdefault(a, []).append(b)
    parent = {src: None}
    frontier = deque([src])
    while frontier and dst not in parent:
        node = frontier.popleft()
        for peer in adjacency.get(node, ()):
            if peer not in parent:
                parent[peer] = node
                frontier.append(peer)
    if dst not in parent:
        return []
    hops = []
    node = dst
    while parent[node] is not None:
        hops.append((parent[node], node))
        node = parent[node]
    return hops[::-1]


def _integrity(receivers: list[dict], app_bytes_delivered: int, all_stats: list[dict]) -> dict:
    """Tamper check on delivered bytes.

    Every application byte a receiver delivers had to arrive inside some
    datagram, so the payload bytes that reached the socket bound the bytes that
    can be delivered. Subtracting the IP and UDP headers makes the bound tight
    enough that forging even a small fraction shows up, because an honest
    protocol also spends bytes on its own headers.

    This is detection, not prevention. A protocol running on the same machine as
    the harness can read files and process arguments.
    """
    wire_in = sum(int(s["counters"].get("wire_bytes_recv", 0)) for s in receivers)
    datagrams_in = sum(int(s["counters"].get("datagrams_recv", 0)) for s in receivers)
    payload_in = wire_in - WIRE_HEADER_BYTES * datagrams_in
    ok = app_bytes_delivered <= payload_in
    return {
        "ok": bool(ok),
        "app_bytes_delivered": app_bytes_delivered,
        "receiver_payload_bytes_recv": payload_in,
        "receiver_datagrams_recv": datagrams_in,
        "note": "" if ok else (
            "delivered more application bytes than arrived in datagram payloads, so "
            "some delivered bytes did not come from the network"
        ),
    }


def _check_requirements(scenario, throughput, latency_ms, reliability, overhead, claims,
                        steps=None, deadline_met=None) -> dict:
    """Evaluate scenario requirements against claimed units."""
    checked: dict[str, dict] = {}
    unchecked: list[str] = []
    reqs = scenario.requirements or {}

    # With nothing delivered, latency and overhead come out as 0, which would
    # pass any "at most" bound. A protocol that delivers nothing meets none.
    delivered_any = reliability.get("delivered", 0) > 0

    def record(name, actual, bound, ok):
        checked[name] = {"actual": round(actual, 6), "bound": bound, "met": bool(ok)}

    for key, spec in reqs.items():
        if not isinstance(spec, dict):
            unchecked.append(key)
            continue
        for cond, bound in spec.items():
            bound = float(bound)
            name = f"{key}.{cond}"
            if key == "latency_ms" and cond.endswith("_max"):
                stat = cond[: -len("_max")]
                actual = latency_ms.get(stat, 0.0) * claims.latency_multiplier
                record(name, actual, bound, delivered_any and actual <= bound)
            elif key == "reliability" and cond.startswith("min_"):
                stat = cond[len("min_"):]
                actual = reliability.get(stat, latency_ms.get(stat, 0.0))
                record(name, actual, bound, actual >= bound)
            elif key == "throughput_bps" and cond.startswith("min_"):
                label = cond[len("min_"):]
                actual = throughput.get(label, 0.0) * claims.throughput_multiplier
                record(name, actual, bound, actual >= bound)
            elif key == "overhead" and cond.startswith("max_"):
                stat = cond[len("max_"):]
                actual = overhead.get(stat, 0.0)
                record(name, actual, bound, delivered_any and actual <= bound)
            elif key == "deadline_met" and cond.startswith("min_"):
                # deadline_met.min_alarm: of the messages labelled alarm, this
                # fraction arrived within their deadline. A workload with no
                # such messages meets it.
                label = cond[len("min_"):]
                actual = (deadline_met or {}).get(label, 1.0)
                record(name, actual, bound, actual >= bound)
            elif key in ("step_slowdown", "step_ms") and cond.endswith("_max") and steps:
                # step_slowdown.mean_max: the mean over rounds of each round's
                # slowdown is at most the bound. Each round already counts its
                # slowest message, so the mean doesn't hide a straggler.
                stat = cond[: -len("_max")]
                actual = steps[key.removeprefix("step_")].get(stat, 0.0)
                if key == "step_ms":
                    actual *= claims.latency_multiplier
                record(name, actual, bound, delivered_any and actual <= bound)
            else:
                unchecked.append(name)

    return {
        "checked": checked,
        "unchecked": unchecked,
        "all_met": all(c["met"] for c in checked.values()) if checked else None,
        "evaluated_in": "claimed units",
    }


def write_timeseries(path: Path, endpoint_stats: list[dict]) -> None:
    """One row per time bucket: delivered app bytes and wire bytes sent."""
    bucket_s = next((s.get("bucket_s", 0.25) for s in endpoint_stats), 0.25)
    app: dict[int, int] = {}
    wire: dict[int, int] = {}
    for s in endpoint_stats:
        for k, v in s.get("app_buckets", {}).items():
            app[int(k)] = app.get(int(k), 0) + int(v)
        for k, v in s.get("wire_buckets", {}).items():
            wire[int(k)] = wire.get(int(k), 0) + int(v)
    buckets = sorted(set(app) | set(wire))
    with Path(path).open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t_s", "app_bytes_delivered", "wire_bytes_sent"])
        for b in buckets:
            w.writerow([round(b * bucket_s, 3), app.get(b, 0), wire.get(b, 0)])


def write_results(path: Path, results: dict) -> None:
    Path(path).write_text(json.dumps(results, indent=2) + "\n")
