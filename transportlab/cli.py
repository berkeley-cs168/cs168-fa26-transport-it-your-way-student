"""Command line front end.

    python -m transportlab list
    python -m transportlab run --scenario leo --workload basic_workload --protocol example/baseline_stopwait
    python -m transportlab report --run-dir runs/leo-basic_workload
    python -m transportlab validate
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import scenario as scenario_mod
from . import report as report_mod
from .orchestrator import prepare, run
from .protocols import ProtocolLoadError
from .trace import WorkloadError, build_trace, load_card, trace_hash


def _cmd_list(args) -> int:
    found = scenario_mod.discover()
    if not found:
        print("no scenarios found", file=sys.stderr)
        return 1
    for s in found.values():
        print(f"{s.id:12s} {s.title}")
        print(f"{'':12s} hosts: {', '.join(s.host_names())}")
        if s.claims.note:
            print(f"{'':12s} scale: {s.claims.note}")
        for name in s.workloads():
            print(f"{'':12s} workload: {name}")
    return 0


def _cmd_run(args) -> int:
    trial = prepare(args.scenario, args.workload, args.protocol, args.out,
                    args.runner, args.seed, args.pcap, args.vis)
    print(f"scenario {trial.scenario.id}  workload {trial.card['id']}  "
          f"messages {len(trial.trace)}  duration {trial.duration_s:.2f}s wire  "
          f"runner {trial.runner_kind}")
    results = run(trial)
    print()
    print(report_mod.summary_text(results))
    if not args.no_plots:
        target = report_mod.render(trial.out_dir)
        print(f"\nplots: {target}" if target else "\nmatplotlib missing, no plots")
    if trial.vis:
        from . import vis as vis_mod
        page = vis_mod.render(trial.out_dir)
        print(f"\nviewer: {page}")
        print(f"        open it in a browser: {vis_mod.open_command(page)}")
    if results.get("pcaps"):
        print(f"\npcaps: {', '.join(results['pcaps'])} in {trial.out_dir}")
        print("       open them in Wireshark")
    print(f"results: {trial.out_dir / 'results.json'}")
    return 0 if results["status"] == "ok" else 1


def _cmd_report(args) -> int:
    target = report_mod.render(args.run_dir)
    results = json.loads((Path(args.run_dir) / "results.json").read_text())
    print(report_mod.summary_text(results))
    if target:
        print(f"\nplots: {target}")
    return 0


def _cmd_validate(args) -> int:
    problems = []
    found = scenario_mod.discover()
    if not found:
        problems.append("no scenarios discovered")
    for s in found.values():
        from .topo import check_connectivity, normalize_links
        try:
            links = normalize_links(s)
            check_connectivity(s, links)
        except Exception as exc:
            problems.append(f"{s.id}: topology: {exc}")
            continue
        cards = s.workloads()
        if not cards:
            problems.append(f"{s.id}: no workload cards")
        for name, path in cards.items():
            try:
                card = load_card(path)
                once = build_trace(card, s.hosts, s.claims.time_factor, s.wire_time_divisor)
                twice = build_trace(card, s.hosts, s.claims.time_factor, s.wire_time_divisor)
                if trace_hash(once) != trace_hash(twice):
                    problems.append(f"{s.id}/{name}: trace is not deterministic")
                from .events import schedule_from_card
                schedule_from_card(card, links, s.card_divisor(card))
                print(f"ok  {s.id}/{name}: {len(once)} messages, "
                      f"{sum(m.size for m in once)} app bytes, "
                      f"fingerprint {trace_hash(once)[:12]}")
            except Exception as exc:
                problems.append(f"{s.id}/{name}: {exc}")
    for p in problems:
        print(f"FAIL {p}", file=sys.stderr)
    return 1 if problems else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="transportlab")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="scenarios and their workloads").set_defaults(fn=_cmd_list)

    r = sub.add_parser("run", help="run one trial")
    r.add_argument("--scenario", required=True)
    r.add_argument("--workload", default="basic_workload")
    r.add_argument("--protocol", required=True)
    r.add_argument("--out", default=None)
    r.add_argument("--runner", default="ns3", choices=["ns3", "loopback"])
    r.add_argument("--seed", type=int, default=None)
    r.add_argument("--no-plots", action="store_true")
    r.add_argument("--pcap", action="store_true",
                   help="write a pcap per network device, for Wireshark")
    r.add_argument("--vis", action="store_true",
                   help="write vis.html, a browser view of the trial")
    r.set_defaults(fn=_cmd_run)

    rep = sub.add_parser("report", help="re-render plots for a run directory")
    rep.add_argument("--run-dir", required=True)
    rep.set_defaults(fn=_cmd_report)

    v = sub.add_parser("validate", help="load every scenario and card, check determinism")
    v.set_defaults(fn=_cmd_validate)

    args = p.parse_args(argv)
    if getattr(args, "out", None) is None and args.cmd == "run":
        args.out = f"runs/{args.scenario}-{args.workload}"
    # A mistake in a workload, a scenario or a protocol file is the student's
    # to fix, and the message says how. A traceback in front of it only hides
    # the one line that matters.
    try:
        return args.fn(args)
    except (WorkloadError, scenario_mod.ScenarioError, ProtocolLoadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
