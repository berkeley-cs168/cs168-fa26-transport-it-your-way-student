"""Run one trial end to end.

Two runners:

- `ns3` (the default, and the only gradeable one): write the network, trace and
  link events to spec.json, run transportlab.simrun in a subprocess, and read
  back the simulator's counts.
- `loopback`: one process per host on 127.0.0.1, real sockets, no shaping. For
  debugging protocol logic over a real network stack.

Either way every endpoint's counters are merged into results.json. A protocol
that crashes, hangs, or sends nothing still produces a parseable results file,
because the grader downstream should never have to special-case a broken
submission.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import protocols
from . import orbit
from .events import link_timeline, schedule_from_card
from .metrics import build_results, write_results, write_timeseries
from .net import BASE_PORT, PCAP_SNAPLEN, make_runner
from .scenario import Scenario, load as load_scenario
from .topo import check_connectivity, normalize_links, sim_params
from .trace import (
    Message,
    WorkloadError,
    build_trace,
    load_card,
    trace_duration,
    trace_hash,
    write_trace,
)

DEFAULT_DRAIN_S = 5.0
KILL_SLACK_S = 10.0

# Wall-clock limit for one ns-3 trial. Simulated time doesn't depend on it; it
# only catches a protocol stuck in a loop inside one callback.
NS3_TIMEOUT_S = float(os.environ.get("TRANSPORTLAB_SIM_TIMEOUT_S", 900))

# Run the simulator, and so the student's code, as this user. The Gradescope
# autograder sets it, so a protocol can't read the hidden workloads or touch
# the files that get scored. Everything that user is running is killed before
# the results are read.
SIM_USER = os.environ.get("TRANSPORTLAB_SIM_USER", "")
RUNNERS = ("ns3", "loopback")


@dataclass
class Trial:
    scenario: Scenario
    card: dict
    trace: list[Message]
    out_dir: Path
    duration_s: float
    runner_kind: str
    protocol: str
    pcap: bool = False
    vis: bool = False
    # No loss and no link events: the autograder's correctness checks, which
    # ask whether a protocol delivers when nothing goes wrong.
    clean: bool = False


def _roles(trace: list[Message]) -> dict[str, str]:
    senders = {m.src for m in trace}
    receivers = {m.dst for m in trace}
    both = senders & receivers
    if both:
        raise WorkloadError(
            f"hosts {sorted(both)} both send and receive. the API has one role per "
            "host, so split them into separate hosts in the topology"
        )
    return {**{h: "sender" for h in senders}, **{h: "receiver" for h in receivers}}


def prepare(
    scenario_id: str,
    workload: str,
    protocol: str,
    out_dir: str | Path,
    runner_kind: str = "ns3",
    seed: int | None = None,
    pcap: bool = False,
    vis: bool = False,
    clean: bool = False,
) -> Trial:
    scenario = load_scenario(scenario_id)
    card = load_card(scenario.workload_path(workload))
    if seed is not None:
        card["seed"] = int(seed)
    trace = build_trace(card, scenario.hosts, scenario.claims.time_factor,
                        scenario.wire_time_divisor)

    divisor = scenario.card_divisor(card)
    if card.get("duration_s"):
        duration = float(card["duration_s"]) / divisor
    else:
        drain = float(card.get("drain_s", DEFAULT_DRAIN_S)) / divisor
        duration = trace_duration(trace) + drain

    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if runner_kind not in RUNNERS:
        raise SystemExit(f"unknown runner {runner_kind!r}, expected one of {RUNNERS}")
    return Trial(scenario, card, trace, out, duration, runner_kind, protocol, pcap, vis,
                 clean)


def run(trial: Trial) -> dict:
    scenario, card, trace, out = trial.scenario, trial.card, trial.trace, trial.out_dir
    trace_path = out / "trace.json"
    write_trace(trace, trace_path)
    fingerprint = trace_hash(trace)
    roles = _roles(trace)
    links = normalize_links(scenario)
    check_connectivity(scenario, links)
    actions = schedule_from_card(card, links, scenario.card_divisor(card))
    if trial.clean:
        actions = []
        for link in links:
            link.params1.pop("loss", None)
            link.params2.pop("loss", None)

    started = time.monotonic()
    if trial.runner_kind == "ns3":
        status, events, stats, extra = _run_ns3(trial, roles, links, actions, trace_path)
        label = extra.pop("label", None) or trial.protocol
    else:
        status, events, stats = _run_loopback(trial, roles, actions, trace_path)
        extra = {}
        label = protocols.label(trial.protocol)
    wall = max(0.0, time.monotonic() - started)

    missing = sorted(set(roles) - {s["endpoint"] for s in stats})
    if missing:
        events.append({"kind": "missing_endpoint_stats", "endpoints": missing})
        if status == "ok":
            status = "crash"

    results = build_results(
        scenario=scenario,
        card=card,
        trace=trace,
        protocol_label=label,
        endpoint_stats=stats,
        duration_s=trial.duration_s,
        status=status,
        events=events,
        trace_fingerprint=fingerprint,
    )
    results["runner"] = trial.runner_kind
    results["parameters"] = scenario.parameters
    results["gradeable"] = trial.runner_kind == "ns3" and not trial.clean
    if trial.clean:
        results["clean_network"] = True
    results.update(extra)
    pcaps = sorted(f.name for f in out.glob("*.pcap"))
    if pcaps:
        results["pcaps"] = pcaps
    write_results(out / "results.json", results)
    write_timeseries(out / "timeseries.csv", stats)
    # Wall-clock facts go in their own file, so results.json is the same
    # byte for byte every time a trial is run with the same seed.
    (out / "run-meta.json").write_text(json.dumps({
        "wall_duration_s": round(wall, 3),
        "python": sys.version.split()[0],
    }, indent=2) + "\n")
    return results


def _sim_spec(trial: Trial, roles: dict, links: list, actions: list) -> dict:
    """The network and link events, in the units transportlab._tlsim takes."""
    scenario = trial.scenario
    hosts = []
    for i, name in enumerate(scenario.hosts):
        hosts.append({"name": name, "role": roles.get(name, ""),
                      "ip": f"10.0.0.{i + 1}", "port": BASE_PORT + i})
    spec_links = []
    for link in links:
        spec_links.append({"a": link.a, "b": link.b,
                           "a_to_b": sim_params(link.params1),
                           "b_to_a": sim_params(link.params2)})
    index_of = {link.name: i for i, link in enumerate(links)}

    # Card events and satellite passes, folded into one change per link
    # direction. See transportlab/events.py.
    sky = orbit.load(scenario)
    states, sat_links, passes = [], {}, []
    if sky:
        states = orbit.states(sky, trial.duration_s, fixed=trial.clean)
        sat_links = orbit.satellite_links(sky, list(index_of))
        if trial.clean:
            passes = [{"satellite": sky.satellites[0], "rise_ns": 0,
                       "set_ns": round(trial.duration_s * 1e9), "fixed": True}]
        else:
            passes = [{"satellite": p.satellite, "rise_ns": round(p.rise_s * 1e9),
                       "set_ns": round(p.set_s * 1e9)}
                      for p in sky.passes(trial.duration_s)]
    sim_events = []
    for change in link_timeline(actions, links, states, sat_links):
        sim_events.append({
            "t_ns": round(change["t_s"] * 1e9), "link": index_of[change["link"]],
            "wire": change["wire"], "params": sim_params(change["params"]),
            "flush": change["flush"], "label": change["label"],
        })
    out = trial.out_dir
    return {
        "seed": int(trial.card.get("seed", 0)),
        "duration_ns": round(trial.duration_s * 1e9),
        "bucket_ns": 250_000_000,
        "hosts": hosts,
        "switches": list(scenario.switches),
        "links": spec_links,
        "events": sim_events,
        # For the viewer only; the simulator reads the events above.
        "satellites": list(sky.satellites) if sky else [],
        "passes": passes,
        "pcap_dir": str(out) if trial.pcap else "",
        "pcap_snaplen": PCAP_SNAPLEN,
        "vis_path": str(out / "vis-events.txt") if trial.vis else "",
    }


def _run_ns3(trial: Trial, roles: dict, links: list, actions: list, trace_path: Path):
    out = trial.out_dir
    events: list[dict] = []
    for action in actions:
        events.append({"t_s": round(action.t_s, 3), "kind": "link_event",
                       "detail": action.describe(), "result": "scheduled"})
    sim = _sim_spec(trial, roles, links, actions)
    for p in sim["passes"]:
        if not p.get("fixed"):
            events.append({"t_s": round(max(0, p["rise_ns"]) / 1e9, 3), "kind": "satellite_pass",
                           "detail": f"{p['satellite']} in view until "
                                     f"{p['set_ns'] / 1e9:.3f} s", "result": "scheduled"})
    peers = {h["name"]: [h["ip"], h["port"]] for h in sim["hosts"]}
    spec = {
        "sim": sim, "seed": sim["seed"], "roles": roles, "peers": peers,
        "protocol": str(trial.protocol), "trace": str(trace_path), "out_dir": str(out),
    }
    spec_path = out / "spec.json"
    spec_path.write_text(json.dumps(spec, indent=1))

    command = [sys.executable, "-m", "transportlab.simrun", str(spec_path)]
    if SIM_USER:
        shutil.chown(out, SIM_USER, SIM_USER)
        for f in out.iterdir():
            shutil.chown(f, SIM_USER, SIM_USER)
        command = ["setpriv", f"--reuid={SIM_USER}", f"--regid={SIM_USER}",
                   "--init-groups", "--"] + command

    status = "ok"
    # Same interpreter, a fixed string hash seed so a protocol iterating over a
    # set of strings does the same thing every run.
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    with (out / "sim.stderr").open("w") as err:
        try:
            proc = subprocess.run(
                command, stdout=err, stderr=subprocess.STDOUT, env=env,
                timeout=NS3_TIMEOUT_S,
            )
            if proc.returncode not in (0, 2):
                status = "crash"
                events.append({"kind": "simulator_exit", "code": proc.returncode,
                               "message": _tail(out / "sim.stderr")})
        except subprocess.TimeoutExpired:
            status = "timeout"
            events.append({"kind": "timeout", "message":
                           f"the simulation ran longer than {NS3_TIMEOUT_S:.0f} s of wall "
                           "time, killed. look for a loop that never returns"})
    if SIM_USER:
        # Anything the protocol left running could still rewrite files.
        subprocess.run(["pkill", "-KILL", "-u", SIM_USER], check=False)

    stats = _collect(out, roles)
    extra: dict = {}
    sim_stats_path = out / "sim-stats.json"
    if sim_stats_path.is_file():
        sim_stats = json.loads(sim_stats_path.read_text())
        _merge_sim_stats(stats, sim_stats)
        extra["label"] = sim_stats.get("_label")
        extra["simulator"] = {"name": "ns-3", "version": sim_stats.get("_ns3_version"),
                              "links": sim_stats.get("_links", {})}
    elif status == "ok":
        status = "crash"
    return status, events, stats, extra


def _merge_sim_stats(stats: list[dict], sim_stats: dict) -> None:
    """Swap the simulator's counts in for the ones Python kept.

    The endpoint files still supply the events (exceptions, notes). Everything
    that is graded comes from the simulator.
    """
    for s in stats:
        mine = sim_stats.get(s["endpoint"])
        if not mine:
            continue
        s["counters"] = mine["counters"]
        s["wire_buckets"] = mine["wire_buckets"]
        s["bucket_s"] = 0.25
        if s.get("role") == "receiver":
            s["samples"] = [
                {"msg_id": i, "latency_s": round(lat / 1e9, 6), "t_s": round(t / 1e9, 6)}
                for i, lat, t in mine["samples_ns"]
            ]
            s["app_buckets"] = mine["app_buckets"]
            s["expected"] = mine["expected"]
        else:
            s["unoffered"] = mine["unoffered"]
        if mine["crashed"]:
            s["crashed"] = True
            s.setdefault("events", []).append(
                {"kind": "protocol_exception", "where": "simulator",
                 "error": mine["error"][-2000:]})


def _tail(path: Path, n: int = 1500) -> str:
    try:
        return path.read_text()[-n:]
    except OSError:
        return ""


def _run_loopback(trial: Trial, roles: dict, actions: list, trace_path: Path):
    scenario, card, out = trial.scenario, trial.card, trial.out_dir
    runner = make_runner(trial.runner_kind, scenario, out)

    status = "ok"
    events: list[dict] = []
    stats: list[dict] = []
    procs: dict[str, object] = {}

    try:
        runner.start()
        for w in runner.warnings:
            events.append({"kind": "runner_warning", "message": w})
        for n in runner.notes:
            events.append({"kind": "runner_note", "message": n})

        if trial.pcap:
            for note in runner.start_capture(sorted(roles)):
                events.append({"kind": "capture", "message": note})

        peers = json.dumps({h: list(runner.addr(h)) for h in scenario.hosts})
        # Python startup is not free, and every endpoint has to be listening
        # before the first scheduled message. Push t0 out far enough that a slow
        # import does not read as a lost packet.
        grace = min(5.0, 1.0 + 0.15 * len(roles))
        t0 = time.monotonic() + grace
        events.append({"kind": "startup_grace_s", "value": round(grace, 3)})

        # Receivers first, so nothing arrives at a closed socket.
        for host in sorted(roles, key=lambda h: roles[h] != "receiver"):
            argv = [
                sys.executable, "-u", "-m", "transportlab.run_endpoint",
                "--role", roles[host], "--name", host,
                "--bind-ip", runner.bind_ip(host),
                "--port", str(runner.ports[host]),
                "--peers", peers,
                "--protocol", trial.protocol,
                "--trace", str(trace_path),
                "--out", str(out),
                "--t0", repr(t0),
                "--until", repr(trial.duration_s),
                "--seed", str(int(card.get("seed", 0))),
            ]
            procs[host] = runner.popen(host, argv)

        for action in actions:
            _sleep_until(t0 + action.t_s)
            note = runner.apply(action)
            events.append({"t_s": round(action.t_s, 3), "kind": "link_event",
                           "detail": action.describe(), "result": note})

        deadline = t0 + trial.duration_s + KILL_SLACK_S
        alive = _wait(procs, deadline)
        if alive:
            status = "timeout"
            events.append({"kind": "timeout", "endpoints": sorted(alive),
                           "message": "endpoints still running past the deadline, killed"})
            for host in alive:
                procs[host].kill()
            _wait(procs, time.monotonic() + 3.0)
    except Exception as exc:  # harness failure, not a protocol bug
        # Recorded rather than raised. A batch over a hundred submissions should
        # not stop because one trial hit a harness bug, and the traceback lands
        # in results.json where it is easy to find.
        status = "error"
        events.append({"kind": "harness_error", "error": repr(exc),
                       "traceback": traceback.format_exc(limit=10)})
    finally:
        for host, proc in procs.items():
            if proc.poll() is None:
                proc.kill()
        runner.stop_capture()
        runner.stop()
        stats = _collect(out, roles)
    return status, events, stats


def _sleep_until(target: float) -> None:
    remaining = target - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _wait(procs: dict, deadline: float) -> list[str]:
    while time.monotonic() < deadline:
        alive = [h for h, p in procs.items() if p.poll() is None]
        if not alive:
            return []
        time.sleep(0.05)
    return [h for h, p in procs.items() if p.poll() is None]


def _collect(out: Path, roles: dict[str, str]) -> list[dict]:
    stats = []
    for host in sorted(roles):
        f = out / f"endpoint-{host}.json"
        if not f.is_file():
            continue
        try:
            stats.append(json.loads(f.read_text()))
        except json.JSONDecodeError:
            stats.append({"endpoint": host, "role": roles[host], "crashed": True,
                          "counters": {}, "events": [{"kind": "unreadable_stats"}]})
    return stats


def run_trial(
    scenario_id: str,
    workload: str,
    protocol: str,
    out_dir: str | Path,
    runner_kind: str = "ns3",
    seed: int | None = None,
    pcap: bool = False,
    vis: bool = False,
    clean: bool = False,
) -> dict:
    return run(prepare(scenario_id, workload, protocol, out_dir, runner_kind, seed,
                       pcap, vis, clean))
