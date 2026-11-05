"""Plots and a text summary for one run directory.

Students read these to explain their results in the Stage 3 reflection, so each
panel answers a question they will be asked: did throughput hold up, where did
the tail go, and what did the network do to you while it happened.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .metrics import percentile


def _load_timeseries(path: Path):
    if not path.is_file():
        return [], [], []
    t, app, wire = [], [], []
    with path.open() as fh:
        for row in csv.DictReader(fh):
            t.append(float(row["t_s"]))
            app.append(int(row["app_bytes_delivered"]))
            wire.append(int(row["wire_bytes_sent"]))
    return t, app, wire


def _latency_samples(run_dir: Path) -> list[float]:
    out = []
    for f in sorted(run_dir.glob("endpoint-*.json")):
        data = json.loads(f.read_text())
        out += [s["latency_s"] * 1000.0 for s in data.get("samples", [])]
    return sorted(out)


def summary_text(results: dict) -> str:
    m = results["metrics"]
    claims = results.get("claims", {})
    lines = [
        f"scenario   {results['scenario']}   workload {results['workload']}",
        f"protocol   {results['protocol']}",
        f"runner     {results.get('runner')}   status {results['status']}",
        *(["local run, no network: these numbers don't predict your grade. "
           "use make run for that"] if results.get("runner") == "loopback" else []),
        f"throughput {m['throughput_bps']['total'] / 1e6:.3f} Mbps wire"
        f"   ({m['throughput_bps']['total'] * claims.get('throughput_multiplier', 1) / 1e6:.3f}"
        " Mbps claimed)",
        f"latency    p50 {m['latency_ms']['p50']:.2f} ms   p99 {m['latency_ms']['p99']:.2f} ms"
        f"   max {m['latency_ms']['max']:.2f} ms wire",
        f"reliability {m['reliability']['delivered']}/{m['reliability']['offered']}"
        f" = {m['reliability']['ratio']:.4f}"
        f"   late {m['reliability']['late']}   corrupt {m['reliability']['corrupt']}",
        f"overhead   {m['overhead']['ratio']:.3f} wire bytes per delivered app byte"
        f"   ({m['overhead']['datagrams_sent']} datagrams)",
    ]
    steps = results.get("scenario_specific", {}).get("steps")
    if steps:
        per_round = "  ".join(f"{r['slowdown']:.2f}x" for r in steps["rounds"])
        lines.append(f"step       {len(steps['rounds'])} rounds, slowdown vs ideal"
                     f" mean {steps['slowdown']['mean']:.2f}x   max {steps['slowdown']['max']:.2f}x")
        lines.append(f"           per round: {per_round}")
    reqs = results.get("requirements", {}).get("checked", {})
    if reqs:
        lines.append("requirements")
        for name, r in sorted(reqs.items()):
            mark = "met " if r["met"] else "MISS"
            lines.append(f"  {mark} {name}: {r['actual']} against {r['bound']}")
    if not results.get("integrity", {}).get("ok", True):
        lines.append("INTEGRITY: " + results["integrity"]["note"])
    return "\n".join(lines)


def render(run_dir: str | Path) -> Path | None:
    """Write plots.png in the run directory. Returns None if matplotlib is absent."""
    run_dir = Path(run_dir)
    results = json.loads((run_dir / "results.json").read_text())
    (run_dir / "summary.txt").write_text(summary_text(results) + "\n")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    t, app, wire = _load_timeseries(run_dir / "timeseries.csv")
    bucket = (t[1] - t[0]) if len(t) > 1 else 0.25
    latencies = _latency_samples(run_dir)

    fig, axes = plt.subplots(3, 1, figsize=(9, 10))
    fig.suptitle(
        f"{results['scenario']} / {results['workload']} / {results['protocol']}",
        fontsize=11,
    )

    ax = axes[0]
    if t:
        ax.plot(t, [b * 8 / bucket / 1e6 for b in app], label="goodput delivered")
        ax.plot(t, [b * 8 / bucket / 1e6 for b in wire], label="wire sent", alpha=0.6)
    ax.set_ylabel("Mbps (wire units)")
    ax.set_xlabel("seconds")
    ax.legend(loc="upper right", fontsize=8)
    ax.set_title("throughput over time", fontsize=9)

    ax = axes[1]
    if latencies:
        ys = [i / len(latencies) for i in range(1, len(latencies) + 1)]
        ax.plot(latencies, ys)
        for p in (50, 95, 99):
            ax.axvline(percentile(latencies, p), linestyle=":", linewidth=0.8)
    ax.set_ylabel("fraction of messages")
    ax.set_xlabel("delivery latency, ms (wire units)")
    ax.set_title("latency CDF, dotted lines at p50 p95 p99", fontsize=9)

    ax = axes[2]
    ax.axis("off")
    ax.text(0, 1, summary_text(results), family="monospace", fontsize=7.5,
            va="top", ha="left", transform=ax.transAxes)
    for e in results.get("events", []):
        if e.get("kind") == "link_event" and "t_s" in e:
            axes[0].axvline(e["t_s"], color="crimson", alpha=0.35, linewidth=0.8)

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    target = run_dir / "plots.png"
    fig.savefig(target, dpi=130)
    plt.close(fig)
    return target
