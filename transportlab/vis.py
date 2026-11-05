"""VIS=1: turn a trial's ns-3 event log into vis.html and netanim.xml.

vis.html is one self-contained page, data and code inline, no network: open it
in any browser, on any platform. It animates packets on the scenario's
network, shows queues, drops and blackouts, and plots queue occupancy and
delivered throughput.

The picture comes from the scenario's `view:` in scenario.yaml: `orbit`
(ground stations on the Earth, satellites crossing overhead), `clos` (a spine and
leaf fabric), `bay` (the San Francisco Bay). Anything else gets a plain
layout. These only change the picture; the simulated links are the same.

netanim.xml is the same packets in NetAnim's format, for anyone who has
NetAnim installed. NetAnim itself is optional; it needs Qt.

The event log (vis-events.txt) is written by sim/tlsim.cc, one event per
line, time in nanoseconds first:

    <t> tx <from> <to> <bytes> <tx_ns> <arrive_ns>   packet starts onto a link
    <t> queue <from> <to> <packets>                  queue length changed
    <t> drop <from> <to> <bytes>                     queue dropped a packet
    <t> event <from> <to> <label> <bps> <queued> <in_flight>   link event
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import scenario as scenario_mod
from .trace import read_trace

# Queue samples kept per link direction, and buckets in the throughput
# chart. Enough for smooth charts, small enough that a long trial's page
# still opens quickly.
QUEUE_POINTS = 4000
THROUGHPUT_BUCKETS = 400
VIEWS = ("orbit", "clos", "bay", "plain")


def _read(run_dir: Path) -> dict:
    spec = json.loads((run_dir / "spec.json").read_text())
    results = json.loads((run_dir / "results.json").read_text())
    sim = spec["sim"]
    links = [{"a": l["a"], "b": l["b"],
              "bps": [l["a_to_b"]["bps"], l["b_to_a"]["bps"]],
              "queue": [l["a_to_b"]["queue_pkts"], l["b_to_a"]["queue_pkts"]]}
             for l in sim["links"]]
    dirs = {}
    for i, l in enumerate(links):
        dirs[(l["a"], l["b"])] = 2 * i
        dirs[(l["b"], l["a"])] = 2 * i + 1

    tx: list[int] = []        # flat: dir, start_us, end_us, bytes
    drops: list[int] = []     # flat: dir, t_us
    queues: dict[int, list[list[int]]] = {d: [] for d in dirs.values()}
    events: list[dict] = []
    path = run_dir / "vis-events.txt"
    if path.is_file():
        with path.open() as f:
            for line in f:
                parts = line.split()
                t = int(parts[0])
                kind = parts[1]
                d = dirs.get((parts[2], parts[3]))
                if d is None:
                    continue
                if kind == "tx":
                    size, arrive = int(parts[4]), int(parts[6])
                    tx += [d, t // 1000, (t + arrive) // 1000, size]
                elif kind == "queue":
                    queues[d].append([t // 1000, int(parts[4])])
                elif kind == "drop":
                    drops += [d, t // 1000]
                elif kind == "event":
                    events.append({"t": t // 1000, "dir": d, "label": parts[4]})

    # Thin out the queue samples: keep the largest value in each time slot,
    # so a short spike to a full queue stays visible.
    duration_us = sim["duration_ns"] // 1000
    thinned = {}
    for d, points in queues.items():
        if not points:
            continue
        if len(points) <= QUEUE_POINTS:
            thinned[d] = points
            continue
        slot = max(1, duration_us // QUEUE_POINTS)
        out: list[list[int]] = []
        for t, n in points:
            key = t // slot * slot
            if out and out[-1][0] == key:
                out[-1][1] = max(out[-1][1], n)
            else:
                out.append([key, n])
        thinned[d] = out

    # Delivered throughput per receiver, from the simulator's own samples.
    throughput = {}
    bucket_us = max(1, duration_us // THROUGHPUT_BUCKETS)
    stats_path = run_dir / "sim-stats.json"
    if stats_path.is_file():
        stats = json.loads(stats_path.read_text())
        sizes = {m.msg_id: m.size for m in read_trace(run_dir / "trace.json")}
        nbuckets = duration_us // bucket_us + 1
        for host, s in stats.items():
            if host.startswith("_") or not s.get("samples_ns"):
                continue
            series = [0] * nbuckets
            for msg_id, _, t in s["samples_ns"]:
                series[min(nbuckets - 1, t // 1000 // bucket_us)] += sizes.get(msg_id, 0)
            throughput[host] = [round(b * 8 / (bucket_us / 1e6) / 1e6, 4) for b in series]

    try:
        scenario = scenario_mod.load(results["scenario"])
        title, view = scenario.title, scenario.manifest.get("view", "plain")
        labels = dict(scenario.manifest.get("view_labels") or {})
    except Exception:
        title, view, labels = results.get("scenario", "trial"), "plain", {}
    if view not in VIEWS:
        view = "plain"

    roles = spec["roles"]
    m = results.get("metrics", {})
    claimed = results.get("claimed", {})
    rel = m.get("reliability", {})
    return {
        "title": title,
        "scenario": results.get("scenario"),
        "workload": results.get("workload"),
        "protocol": results.get("protocol"),
        "status": results.get("status"),
        "view": view,
        "labels": labels,
        "summary": {
            "delivered": rel.get("delivered", 0),
            "offered": rel.get("offered", 0),
            "throughput_bps": claimed.get("throughput_bps", {}).get("active", 0),
            "p99_ms": claimed.get("latency_ms", {}).get("p99", 0),
            "overhead": m.get("overhead", {}).get("ratio", 0),
        },
        "duration_us": duration_us,
        "nodes": [{"name": h["name"], "kind": "host", "role": roles.get(h["name"], ""),
                   "ip": h["ip"]} for h in sim["hosts"]]
                 + [{"name": s, "kind": "switch", "role": "", "ip": ""}
                    for s in sim["switches"]],
        "links": links,
        "tx": tx,
        "drops": drops,
        "queues": thinned,
        "events": events,
        "satellites": sim.get("satellites", []),
        "passes": [{"sat": x["satellite"], "rise": x["rise_ns"] // 1000, "set": x["set_ns"] // 1000,
                    "fixed": bool(x.get("fixed"))} for x in sim.get("passes", [])],
        "throughput": throughput,
        "throughput_bucket_us": bucket_us,
    }


def _netanim(data: dict, path: Path) -> None:
    """NetAnim's format: nodes on a grid, then one line per packet."""
    ids = {n["name"]: i for i, n in enumerate(data["nodes"])}
    lines = ['<anim ver="netanim-3.109" filetype="animation" >']
    for i, n in enumerate(data["nodes"]):
        lines.append(f'<node id="{i}" sysId="0" locX="{20 * (i % 6):.2f}" '
                     f'locY="{20 * (i // 6):.2f}" />')
        lines.append(f'<nu p="d" t="0" id="{i}" descr="{n["name"]}" />')
    for l in data["links"]:
        lines.append(f'<link fromId="{ids[l["a"]]}" toId="{ids[l["b"]]}" fd="" td="" ld="" />')
    tx = data["tx"]
    for k in range(0, len(tx), 4):
        d, start, end, _ = tx[k:k + 4]
        link = data["links"][d // 2]
        a, b = (link["a"], link["b"]) if d % 2 == 0 else (link["b"], link["a"])
        # The log has when the packet starts and when its last bit arrives.
        # NetAnim wants first and last bit at each end; with no serialization
        # time recorded separately, the packet is drawn as a point.
        lines.append(f'<p fId="{ids[a]}" fbTx="{start / 1e6:.9f}" lbTx="{start / 1e6:.9f}" '
                     f'tId="{ids[b]}" fbRx="{end / 1e6:.9f}" lbRx="{end / 1e6:.9f}" />')
    lines.append("</anim>")
    path.write_text("\n".join(lines) + "\n")


def render(run_dir: str | Path) -> Path:
    run_dir = Path(run_dir)
    data = _read(run_dir)
    page = _PAGE.replace("__DATA__", json.dumps(data, separators=(",", ":")))
    target = run_dir / "vis.html"
    target.write_text(page)
    _netanim(data, run_dir / "netanim.xml")
    return target


def open_command(page: Path) -> str:
    """The command that opens the page in a browser on this computer."""
    try:
        if "microsoft" in Path("/proc/version").read_text().lower():
            return f'explorer.exe "$(wslpath -w {page})"'
    except OSError:
        pass
    if os.uname().sysname == "Darwin":
        return f"open {page}"
    return f"xdg-open {page}"


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trial viewer</title>
<style>
:root {
  --bg: #f6f7f9; --card: #ffffff; --fg: #15171a; --muted: #6b7280; --line: #e3e6ea;
  --accent: #2563eb; --data: #2563eb; --ack: #0e9f6e; --drop: #dc2626; --queue: #d97706;
  --dark: #7f1d1d; --node: #15171a; --chip: #eef1f5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1115; --card: #171a20; --fg: #e8eaee; --muted: #9aa3af; --line: #2a2f38;
    --accent: #60a5fa; --data: #60a5fa; --ack: #34d399; --drop: #f87171; --queue: #fbbf24;
    --dark: #be123c; --node: #e8eaee; --chip: #222731;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.45 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 24px 16px 40px; }
header { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: baseline;
  justify-content: space-between; margin-bottom: 14px; }
h1 { font-size: 20px; font-weight: 650; margin: 0; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 13px; }
.status { font-size: 12px; font-weight: 600; padding: 2px 10px; border-radius: 99px;
  background: var(--chip); }
.status.ok { color: var(--ack); } .status.bad { color: var(--drop); }
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 14px; }
.stat { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; }
.stat b { display: block; font-size: 18px; font-weight: 650; font-variant-numeric: tabular-nums; }
.stat span { color: var(--muted); font-size: 12px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
.controls { display: flex; align-items: center; gap: 12px; padding: 10px 14px;
  border-bottom: 1px solid var(--line); flex-wrap: wrap; }
#play { width: 34px; height: 34px; border-radius: 50%; border: 0; background: var(--accent);
  color: #fff; cursor: pointer; display: grid; place-items: center; flex: none; }
#play svg { width: 14px; height: 14px; fill: currentColor; }
.speeds { display: flex; background: var(--chip); border-radius: 8px; padding: 2px; }
.speeds button { border: 0; background: none; color: var(--muted); font: inherit; font-size: 12px;
  padding: 4px 9px; border-radius: 6px; cursor: pointer; }
.speeds button.on { background: var(--card); color: var(--fg); box-shadow: 0 1px 2px #0002; }
.track { position: relative; flex: 1; min-width: 160px; height: 22px; }
.track .bands { position: absolute; left: 0; right: 0; top: 8px; height: 6px; border-radius: 3px;
  background: var(--chip); overflow: hidden; }
.track .bands i { position: absolute; top: 0; bottom: 0; background: var(--dark); opacity: .55; }
#scrub { position: absolute; inset: 0; width: 100%; margin: 0; background: none;
  -webkit-appearance: none; appearance: none; }
#scrub::-webkit-slider-thumb { -webkit-appearance: none; width: 14px; height: 14px; border-radius: 50%;
  background: var(--accent); border: 2px solid var(--card); box-shadow: 0 0 0 1px var(--accent); }
#scrub::-moz-range-thumb { width: 12px; height: 12px; border-radius: 50%; background: var(--accent);
  border: 2px solid var(--card); }
#scrub::-moz-range-track { background: none; }
.clock { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted);
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.clock b { color: var(--fg); font-weight: 600; }
canvas { display: block; width: 100%; }
#net { height: 460px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; padding: 10px 14px; color: var(--muted);
  font-size: 12px; border-top: 1px solid var(--line); }
.legend i { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px;
  vertical-align: -1px; }
.legend .x { color: var(--drop); font-weight: 700; margin-right: 5px; }
.legend .note { margin-left: auto; }
.charts { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }
.chart h2 { font-size: 13px; font-weight: 600; margin: 0; padding: 12px 14px 0; }
.chart .keys { display: flex; flex-wrap: wrap; gap: 4px 12px; padding: 4px 14px 0; font-size: 12px;
  color: var(--muted); }
.chart .keys i { display: inline-block; width: 10px; height: 3px; border-radius: 2px; margin-right: 5px;
  vertical-align: 3px; }
.chart canvas { height: 170px; }
@media (max-width: 760px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  .charts { grid-template-columns: 1fr; }
  #net { height: 360px; }
  .legend .note { margin-left: 0; }
}
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1 id="title"></h1>
      <div class="sub" id="sub"></div>
    </div>
    <span class="status" id="status"></span>
  </header>
  <section class="stats" id="stats"></section>
  <section class="card">
    <div class="controls">
      <button id="play" aria-label="Play"></button>
      <div class="speeds" id="speeds"></div>
      <div class="track"><div class="bands" id="bands"></div>
        <input type="range" id="scrub" min="0" max="1000" value="0" aria-label="Time"></div>
      <span class="clock" id="clock"></span>
    </div>
    <canvas id="net"></canvas>
    <div class="legend">
      <span><i style="background:var(--data)"></i>data packet</span>
      <span><i style="background:var(--ack)"></i>small packet (ack)</span>
      <span><i style="background:var(--queue)"></i>queue filling</span>
      <span><b class="x">&#x2715;</b>drop</span>
      <span><i style="background:var(--dark)"></i>blackout</span>
      <span class="note" id="note"></span>
    </div>
  </section>
  <section class="charts">
    <div class="card chart"><h2>Queue occupancy, packets</h2><div class="keys" id="qkeys"></div>
      <canvas id="queues"></canvas></div>
    <div class="card chart"><h2 id="ttitle">Delivered throughput</h2><div class="keys" id="tkeys"></div>
      <canvas id="tput"></canvas></div>
  </section>
</main>
<script>
"use strict";
const D = __DATA__;
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const END = D.duration_us;
const SERIES = ["#2563eb", "#d97706", "#0e9f6e", "#9333ea", "#dc2626", "#0891b2", "#65a30d", "#db2777"];

// ---- header --------------------------------------------------------------

document.getElementById("title").textContent = D.title;
document.getElementById("sub").textContent =
  `${D.scenario} / ${D.workload} · ${D.protocol || ""}`;
const status = document.getElementById("status");
status.textContent = D.status;
status.className = "status " + (D.status === "ok" ? "ok" : "bad");

function fmtRate(bps) {
  if (bps >= 1e9) return (bps / 1e9).toFixed(2) + " Gbps";
  if (bps >= 1e6) return (bps / 1e6).toFixed(2) + " Mbps";
  if (bps >= 1e3) return (bps / 1e3).toFixed(2) + " kbps";
  return bps.toFixed(1) + " bps";
}
function fmtMs(ms) { return ms >= 10000 ? (ms / 1000).toFixed(1) + " s" : ms.toFixed(ms < 10 ? 2 : 0) + " ms"; }
const S = D.summary;
document.getElementById("stats").innerHTML = [
  [`${S.delivered} / ${S.offered}`, "messages delivered"],
  [fmtRate(S.throughput_bps), "throughput, active window"],
  [fmtMs(S.p99_ms), "p99 latency"],
  [S.overhead.toFixed(2), "wire bytes per delivered byte"],
].map(([v, k]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");

// ---- data indexes ---------------------------------------------------------

const N = D.tx.length / 4;
const order = [...Array(N).keys()].sort((a, b) => D.tx[4 * a + 1] - D.tx[4 * b + 1]);
let maxFlight = 1;
for (let i = 0; i < N; i++) maxFlight = Math.max(maxFlight, D.tx[4 * i + 2] - D.tx[4 * i + 1]);
function firstTxAfter(t) {
  let lo = 0, hi = order.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (D.tx[4 * order[m] + 1] < t) lo = m + 1; else hi = m; }
  return lo;
}
const dropTimes = [];
for (let k = 0; k < D.drops.length; k += 2) dropTimes.push([D.drops[k + 1], D.drops[k]]);
dropTimes.sort((a, b) => a[0] - b[0]);
function dropsBefore(t) {
  let lo = 0, hi = dropTimes.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (dropTimes[m][0] <= t) lo = m + 1; else hi = m; }
  return lo;
}

// Blackout windows per link (either direction).
const dark = {};
for (const e of D.events) {
  const l = e.dir >> 1, spans = (dark[l] = dark[l] || []), open = spans.length && spans[spans.length - 1][1] === END;
  // Each direction logs its own start and end; one window per link.
  if (e.label === "blackout" && !open) spans.push([e.t, END]);
  if (e.label === "blackout_end" && open) spans[spans.length - 1][1] = e.t;
}
const isDark = (l, t) => (dark[l] || []).some(([a, b]) => t >= a && t < b);
const anyDark = t => Object.keys(dark).some(l => isDark(+l, t));

function queueAt(d, t) {
  const q = D.queues[d];
  if (!q) return 0;
  let lo = 0, hi = q.length - 1, v = 0;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (q[m][0] <= t) { v = q[m][1]; lo = m + 1; } else hi = m - 1; }
  return v;
}

const nodes = Object.fromEntries(D.nodes.map(n => [n.name, n]));
const hosts = D.nodes.filter(n => n.kind === "host");
const switches = D.nodes.filter(n => n.kind === "switch");
const neighbours = name => D.links.flatMap(l => l.a === name ? [l.b] : l.b === name ? [l.a] : []);
const hostsOf = sw => neighbours(sw).filter(n => nodes[n] && nodes[n].kind === "host");

// ---- scenes ---------------------------------------------------------------
// Each scene places the nodes, gives every link a path (a list of points
// from a to b), and paints a backdrop. Packets move along the paths.

function stars(ctx, w, h, count) {
  let seed = 7;
  const rand = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  for (let i = 0; i < count; i++) {
    ctx.globalAlpha = 0.25 + 0.6 * rand();
    ctx.fillStyle = "#fff";
    ctx.fillRect(rand() * w, rand() * h * 0.75, rand() < 0.1 ? 2 : 1, rand() < 0.1 ? 2 : 1);
  }
  ctx.globalAlpha = 1;
}

const scenes = {
  // LEO: ground sites on the Earth's curve, satellites crossing overhead.
  // With a constellation (D.passes), each satellite moves left to right
  // across the sky during its pass and its links follow it; outside its pass
  // it is below the horizon and its links aren't drawn. Without one, a link
  // between two switches goes up to a single satellite and back down.
  orbit(w, h) {
    const R = Math.max(w * 0.9, h * 1.4), cx = w / 2, cy = h * 0.66 + R;
    const top = [w / 2, h * 0.17];
    const orbitR = cy - top[1];
    const surface = (angle, lift = 0) =>
      [cx + (R + lift) * Math.sin(angle), cy - (R + lift) * Math.cos(angle)];
    const sats = D.satellites || [];
    const isSat = name => sats.includes(name);
    const grounded = switches.filter(s => !isSat(s.name));
    const pos = {};
    const span = Math.min(0.32, 0.6 * w / R);
    grounded.forEach((s, i) => {
      const a = grounded.length === 1 ? 0 : -span + (2 * span * i) / (grounded.length - 1);
      pos[s.name] = surface(a, 18);
      hostsOf(s.name).forEach((hname, j, all) => {
        const off = (j - (all.length - 1) / 2) * 0.07;
        pos[hname] = surface(a + off + (off >= 0 ? 0.02 : -0.02), -26);
      });
    });
    hosts.forEach((n, i) => { if (!pos[n.name]) pos[n.name] = surface(-span + 0.05 * i, -26); });

    // Half the arc a satellite crosses while in view, and where it is at t:
    // its angle runs from -A at rise to +A at set, at a steady rate, so it
    // slides in from the left before its pass and out to the right after.
    const A = Math.min(0.5, Math.asin(Math.min(0.95, 0.34 * w / orbitR)));
    const passesOf = Object.fromEntries(sats.map(s => [s, D.passes.filter(p => p.sat === s)]));
    function satAngle(name, t) {
      const ps = passesOf[name];
      if (!ps.length) return 10;  // never rises in this trial
      let best = null;
      for (const p of ps) {
        if (p.fixed) return 0;
        const mid = (p.rise + p.set) / 2;
        if (!best || Math.abs(t - mid) < Math.abs(t - (best.rise + best.set) / 2)) best = p;
      }
      const mid = (best.rise + best.set) / 2;
      return ((t - mid) / (best.set - best.rise)) * 2 * A;
    }
    const inView = (name, t) => passesOf[name].some(p => t >= p.rise && t < p.set);
    const satPos = (name, t) => {
      const a = satAngle(name, t);
      return [cx + orbitR * Math.sin(a), cy - orbitR * Math.cos(a)];
    };
    const moving = sats.length > 0;
    const path = (a, b, t = 0) => {
      const at = n => isSat(n) ? satPos(n, t) : pos[n];
      if (!moving && nodes[a].kind === "switch" && nodes[b].kind === "switch") return [pos[a], top, pos[b]];
      return [at(a), at(b)];
    };

    function drawSat(ctx, x, y, alpha, label) {
      ctx.save(); ctx.translate(x, y); ctx.globalAlpha = alpha;
      ctx.fillStyle = "#7aa7ff"; ctx.fillRect(-28, -5, 18, 10); ctx.fillRect(10, -5, 18, 10);
      ctx.fillStyle = "#e8ecf5"; ctx.fillRect(-8, -8, 16, 16);
      ctx.fillStyle = "#a9b4c9"; ctx.fillRect(-10, -1, 2, 2); ctx.fillRect(8, -1, 2, 2);
      ctx.restore();
      ctx.globalAlpha = Math.max(alpha, 0.5);
      ctx.fillStyle = "rgba(220,230,255,.85)"; ctx.font = "12px sans-serif"; ctx.textAlign = "center";
      ctx.fillText(label, x, y - 16);
      ctx.globalAlpha = 1;
    }

    function backdrop(ctx, t) {
      const sky = ctx.createLinearGradient(0, 0, 0, h);
      sky.addColorStop(0, "#050b1a"); sky.addColorStop(1, "#13284d");
      ctx.fillStyle = sky; ctx.fillRect(0, 0, w, h);
      stars(ctx, w, h, 140);
      // orbit
      ctx.strokeStyle = "rgba(160,190,255,.35)"; ctx.setLineDash([4, 6]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(cx, cy, orbitR, Math.PI * 1.5 - A * 1.6, Math.PI * 1.5 + A * 1.6); ctx.stroke();
      ctx.setLineDash([]);
      // satellites, drawn before the Earth so one below the horizon is hidden
      if (moving) {
        for (const s of sats) {
          const a = satAngle(s, t);
          if (Math.abs(a) > A * 1.35) continue;
          const [x, y] = satPos(s, t), up = inView(s, t);
          drawSat(ctx, x, y, up ? (anyDark(t) ? 0.45 : 1) : 0.3, up ? s : s + " · out of view");
        }
      }
      // atmosphere and Earth
      const glow = ctx.createRadialGradient(cx, cy, R * 0.98, cx, cy, R * 1.06);
      glow.addColorStop(0, "rgba(90,160,255,.55)"); glow.addColorStop(1, "rgba(90,160,255,0)");
      ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(cx, cy, R * 1.06, 0, 7); ctx.fill();
      const earth = ctx.createRadialGradient(cx, cy - R, R * 0.1, cx, cy, R);
      earth.addColorStop(0, "#2f7fd6"); earth.addColorStop(0.6, "#1b4f99"); earth.addColorStop(1, "#0d2f66");
      ctx.fillStyle = earth; ctx.beginPath(); ctx.arc(cx, cy, R, 0, 7); ctx.fill();
      // a few land masses, so it reads as the Earth
      ctx.fillStyle = "rgba(76,140,90,.55)";
      for (const [a, wide, deep] of [[-0.22, 0.09, 50], [0.05, 0.06, 35], [0.27, 0.11, 60]]) {
        ctx.beginPath();
        for (let k = 0; k <= 16; k++) {
          const ang = a - wide + (2 * wide * k) / 16;
          const r = R - 4 - deep * Math.sin((Math.PI * k) / 16) * (0.7 + 0.3 * Math.cos(k * 1.7));
          const [x, y] = [cx + r * Math.sin(ang), cy - r * Math.cos(ang)];
          k ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        }
        ctx.closePath(); ctx.fill();
      }
      if (moving) {
        if (!sats.some(s => inView(s, t))) {
          ctx.fillStyle = "#fda4af"; ctx.font = "600 13px sans-serif"; ctx.textAlign = "center";
          ctx.fillText("handover: no satellite in view", top[0], top[1] + 40);
        }
        return;
      }
      // one satellite: dims during a blackout
      const off = anyDark(t);
      drawSat(ctx, top[0], top[1], off ? 0.35 : 1, off ? "satellite out of view" : "satellite");
    }
    return { pos, path, backdrop, dark: true, moving,
      hidden: (l, t) => moving && [l.a, l.b].some(n => isSat(n) && !inView(n, t)),
      label: n => n.name,
      note: moving
        ? "Simulated: one satellite at a time relays between the ground station and the terminal. Each is in view for a pass, then hands over to the next."
        : "Simulated: the ground–terminal link is the satellite hop." };
  },

  // Data center: racks of servers. Each switch with hosts under it is a
  // rack's top-of-rack switch, drawn as the top unit of a cabinet, with a
  // 2U box per server below it. A server is senderN and receiverN: its send
  // port is cabled up the left side of the rack to the switch, its receive
  // port up the right, each server in its own lane, so every link can be
  // followed by eye and none cross. A switch with no servers is the spine,
  // above the racks; a link from a rack to it goes straight up, then across.
  // A link straight between two racks goes up over the spine. Packets ride
  // the cables.
  clos(w, h) {
    const racks = switches.filter(s => hostsOf(s.name).length).map(s => s.name);
    const spines = switches.filter(s => !hostsOf(s.name).length).map(s => s.name);
    const pos = {}, cables = {}, own = new Set();
    const server = name => (/(\d+)$/.exec(name) || [, name])[1];
    const rackW = Math.min(240, w * (racks.length > 2 ? 0.2 : 0.26)), unitH = 46, laneGap = 9;
    const rackX = i => w * (racks.length === 1 ? 0.5 : 0.2 + (0.6 * i) / (racks.length - 1));
    const top = h * 0.2, torH = 26;
    const units = {};                    // rack -> [server numbers]
    racks.forEach((r, i) => {
      const x = rackX(i);
      pos[r] = [x, top + torH / 2];
      own.add(r);
      const nums = [...new Set(hostsOf(r).map(server))].sort((a, b) => a - b);
      units[r] = nums;
      nums.forEach((num, k) => {
        const y = top + torH + 16 + k * (unitH + 12) + unitH / 2;
        for (const [name, dx] of [["sender" + num, -0.3], ["receiver" + num, 0.3]]) {
          if (!nodes[name]) continue;
          pos[name] = [x + dx * rackW, y + 6];
          own.add(name);
        }
      });
    });
    // Cables: from a port out past the cabinet's side into its own lane, up
    // the lane, and in through the side of the switch. Senders on the left,
    // receivers on the right. The top server's lane is the innermost and
    // enters the switch lowest, so no two cables cross.
    const torY = top + torH / 2;
    racks.forEach((r, i) => {
      const x = rackX(i);
      units[r].forEach((num, k) => {
        for (const [name, dir] of [["sender" + num, -1], ["receiver" + num, 1]]) {
          if (!pos[name]) continue;
          const [px, py] = pos[name];
          const lane = x + dir * (rackW / 2 + 16 + k * laneGap);
          const enter = torY + 7 - k * 6;
          const port = x + dir * (rackW / 2 - 10);
          cables[name] = [[px, py], [lane, py], [lane, enter], [port, enter]];
        }
      });
    });
    const spine = [w / 2, h * 0.07];
    spines.forEach(s => { pos[s] = spine; own.add(s); });
    const up = r => [[pos[r][0], pos[r][1] - torH / 2], [pos[r][0], spine[1] + 8], spine];
    const path = (a, b) => {
      if (cables[a]) return cables[a].slice();
      if (cables[b]) return cables[b].slice().reverse();
      if (racks.includes(a) && spines.includes(b)) return up(a);
      if (spines.includes(a) && racks.includes(b)) return up(b).reverse();
      if (racks.includes(a) && racks.includes(b))
        return [[pos[a][0], pos[a][1] - torH / 2], [pos[a][0], spine[1] + 8], spine, [pos[b][0], spine[1] + 8], [pos[b][0], pos[b][1] - torH / 2]];
      return [pos[a], pos[b]];
    };
    const colour = l => {
      const n = [l.a, l.b].find(x => nodes[x] && nodes[x].kind === "host");
      if (!n) return "#e8a33d";                                 // between racks
      return nodes[n].role === "sender" ? "#4f8cff" : "#3ccf91"; // send, receive
    };

    function unit(ctx, x, y, wide, tall, fill) {
      ctx.fillStyle = fill; ctx.strokeStyle = "rgba(255,255,255,.12)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.roundRect(x - wide / 2, y - tall / 2, wide, tall, 4); ctx.fill(); ctx.stroke();
    }
    function backdrop(ctx, t) {
      // the data hall: dark, with a tiled floor
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, "#151a22"); g.addColorStop(1, "#1d2430");
      ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
      ctx.strokeStyle = "rgba(255,255,255,.04)"; ctx.lineWidth = 1;
      for (let y = h * 0.93; y < h; y += 8) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
      for (let x = 0; x < w; x += 40) { ctx.beginPath(); ctx.moveTo(x, h * 0.93); ctx.lineTo(x + (x - w / 2) * 0.1, h); ctx.stroke(); }
      // the spine
      if (racks.length > 1 || spines.length) {
        unit(ctx, spine[0], spine[1], 120, 22, "#2c3442");
        ctx.fillStyle = "#c9d3e3"; ctx.font = "600 11px sans-serif"; ctx.textAlign = "center";
        ctx.fillText((D.labels || {})[spines[0]] || "spine", spine[0], spine[1] - 16);
      }
      racks.forEach((r, i) => {
        const x = rackX(i), n = units[r].length;
        const bottom = top + torH + 16 + n * (unitH + 12) + 8;
        // the cabinet
        ctx.fillStyle = "#0f1319"; ctx.strokeStyle = "#3a4352"; ctx.lineWidth = 3;
        ctx.beginPath(); ctx.roundRect(x - rackW / 2 - 8, top - torH / 2 - 10, rackW + 16, bottom - top + torH / 2 + 18, 10);
        ctx.fill(); ctx.stroke();
        // feet
        ctx.fillStyle = "#3a4352";
        ctx.fillRect(x - rackW / 2, bottom + 8, 14, 6); ctx.fillRect(x + rackW / 2 - 14, bottom + 8, 14, 6);
        // the top-of-rack switch: a 1U box with a row of port lights
        unit(ctx, x, top + torH / 2, rackW - 12, torH, "#2c3442");
        units[r].forEach((num, k) => {
          for (const dir of [-1, 1]) {
            const px = x + dir * (rackW / 2 - 10), py = torY + 7 - k * 6;
            ctx.fillStyle = "#11161d"; ctx.fillRect(px - 4, py - 2.5, 8, 5);
          }
        });
        for (let k = 0; k < 8; k++) {
          const on = Math.floor(t / 2e5 + k * 1.7) % 3 !== 0;
          ctx.fillStyle = on ? "#7CFC9A" : "#2f5a3a";
          ctx.fillRect(x - 28 + k * 8, torY - 2, 3, 3);
        }
        ctx.fillStyle = "#c9d3e3"; ctx.font = "600 11px sans-serif"; ctx.textAlign = "left";
        ctx.fillText((D.labels || {})[r] || r, x - rackW / 2 + 6, top - torH / 2 - 16);
        // servers: a 2U box each, with vents, a power light, and two ports
        units[r].forEach((num, k) => {
          const y = top + torH + 16 + k * (unitH + 12) + unitH / 2;
          unit(ctx, x, y, rackW - 12, unitH, "#252c38");
          ctx.strokeStyle = "rgba(255,255,255,.07)";
          for (let v = 0; v < 6; v++) {
            const vx = x - rackW / 2 + 16 + v * 6;
            ctx.beginPath(); ctx.moveTo(vx, y - 12); ctx.lineTo(vx, y + 12); ctx.stroke();
          }
          ctx.fillStyle = Math.floor(t / 7e5 + k) % 4 ? "#59d0ff" : "#2a6a85";
          ctx.beginPath(); ctx.arc(x + rackW / 2 - 18, y - 12, 2.5, 0, 7); ctx.fill();
          ctx.fillStyle = "#c9d3e3"; ctx.font = "600 11px sans-serif"; ctx.textAlign = "left";
          ctx.fillText("server " + num, x - 24, y - 8);
          ctx.textAlign = "center";
          for (const [name, label] of [["sender" + num, "send"], ["receiver" + num, "recv"]]) {
            if (!pos[name]) continue;
            const [px, py] = pos[name];
            ctx.fillStyle = "#11161d"; ctx.fillRect(px - 7, py - 5, 14, 10);
            ctx.fillStyle = nodes[name].role === "sender" ? "#4f8cff" : "#3ccf91";
            ctx.fillRect(px - 4, py - 2, 8, 4);
            ctx.fillStyle = "rgba(201,211,227,.8)"; ctx.font = "10px sans-serif";
            ctx.fillText(name, px, py + 17);
          }
        });
      });
    }
    // A server cable is port, lane bottom, lane top, switch port (or the
    // reverse), so the lane is always its second segment. Put the gauge
    // there, outside the cabinet, starting at the end the queue drains from.
    const gaugeSpan = r => {
      if (r.p.length !== 4) return null;
      const total = r.len[3], a = r.len[1], b = r.len[2];
      const fromStart = a + 4, length = Math.min(60, b - a - 8);
      return length > 10 ? [fromStart / total, (fromStart + length) / total] : null;
    };
    return { pos, path, backdrop, dark: true, linkColor: colour, gaugeSpan, gaugeLane: 0,
      ownsMarker: n => own.has(n.name), ownsLabel: n => own.has(n.name),
      label: n => n.name,
      note: racks.length > 1
        ? "Each server is a sender and a receiver: its send cable runs up the left of the rack (blue), its receive cable up the right (green). Racks meet over the spine."
        : "Each server is a sender and a receiver, cabled to its rack's switch." };
  },

  // Sensor buoys: a postcard of the Bay. The Golden Gate in the fog on the
  // left, the Berkeley hills on the right with the Campanile as the radio
  // gateway and the lab beyond it, and the buoys bobbing in the water. Only
  // the picture: every buoy has one radio link to the gateway, and the
  // gateway one link to the lab.
  bay(w, h) {
    const s = Math.min(w / 1000, h / 600);
    const X = f => f * w, Y = f => f * h;
    const sea = Y(0.5);                       // the horizon, where the water starts
    const pos = {};
    // Buoys in the foreground water, left to right.
    const spots = [[0.13, 0.72], [0.31, 0.82], [0.47, 0.70], [0.58, 0.83], [0.22, 0.62], [0.40, 0.62]];
    const buoys = hosts.filter(n => n.role === "sender").map(n => n.name);
    buoys.forEach((name, i) => { const [fx, fy] = spots[i % spots.length]; pos[name] = [X(fx), Y(fy) - 40 * s]; });
    // Berkeley hills: the ground rises to the right of hillX.
    const hillX = 0.64, hill = f => Y(0.5) - Y(0.24) * Math.pow(Math.max(0, (f - hillX) / (1 - hillX)), 0.8);
    const towerX = 0.83, towerBase = hill(towerX), towerH = Y(0.23);
    const sw = switches[0] ? switches[0].name : null;
    if (sw) pos[sw] = [X(towerX), towerBase - towerH - 10 * s];      // the antenna on top
    const shore = hosts.filter(n => n.role !== "sender").map(n => n.name);
    // The lab, up the hill past the Campanile.
    shore.forEach((name, j) => { pos[name] = [X(0.93 - 0.06 * j), hill(0.93 - 0.06 * j) - 14 * s]; });
    const path = (a, b2) => [pos[a], pos[b2]];
    const own = new Set([...buoys, ...shore, ...(sw ? [sw] : [])]);

    const ellipse = (ctx, x, y, rx, ry) => { ctx.beginPath(); ctx.ellipse(x, y, rx, ry, 0, 0, 7); ctx.fill(); };
    function cloud(ctx, x, y, r, alpha) {
      ctx.fillStyle = `rgba(255,255,255,${alpha})`;
      for (const [dx, dy, k] of [[0, 0, 1], [0.9, 0.15, 0.8], [-0.9, 0.2, 0.75], [0.4, -0.45, 0.8], [-0.4, -0.35, 0.7]])
        ellipse(ctx, x + dx * r, y + dy * r, r * k, r * k * 0.8);
    }
    function goldenGate(ctx, t) {
      const deck = sea - 22 * s, t1 = X(0.09), t2 = X(0.27), top = sea - Y(0.21), orange = "#d9542b";
      // Marin headlands behind
      ctx.fillStyle = "#86b37a";
      ctx.beginPath(); ctx.moveTo(0, sea);
      ctx.bezierCurveTo(X(0.0), Y(0.33), X(0.06), Y(0.30), X(0.12), Y(0.40));
      ctx.bezierCurveTo(X(0.16), Y(0.45), X(0.2), Y(0.46), X(0.24), sea); ctx.closePath(); ctx.fill();
      ctx.fillStyle = "#9cc58c";
      ctx.beginPath(); ctx.moveTo(X(0.25), sea); ctx.bezierCurveTo(X(0.28), Y(0.45), X(0.31), Y(0.45), X(0.34), sea); ctx.fill();
      // deck and cables
      ctx.strokeStyle = orange; ctx.lineWidth = 4 * s;
      ctx.beginPath(); ctx.moveTo(X(-0.01), deck); ctx.lineTo(X(0.36), deck); ctx.stroke();
      ctx.lineWidth = 2 * s;
      ctx.beginPath(); ctx.moveTo(X(-0.01), deck - 4 * s);
      ctx.quadraticCurveTo((X(-0.01) + t1) / 2, deck - 10 * s, t1, top + 6 * s);
      ctx.quadraticCurveTo((t1 + t2) / 2, deck + 34 * s, t2, top + 6 * s);
      ctx.quadraticCurveTo((t2 + X(0.36)) / 2, deck - 10 * s, X(0.36), deck - 4 * s); ctx.stroke();
      // hangers
      ctx.lineWidth = 0.8 * s; ctx.globalAlpha = 0.8;
      for (let k = 1; k < 16; k++) {
        const f = k / 16, x = t1 + (t2 - t1) * f, cy = top + 6 * s + (deck + 34 * s - top - 6 * s) * 2 * f * (1 - f);
        ctx.beginPath(); ctx.moveTo(x, cy); ctx.lineTo(x, deck); ctx.stroke();
      }
      ctx.globalAlpha = 1;
      // towers
      for (const x of [t1, t2]) {
        ctx.fillStyle = orange;
        ctx.fillRect(x - 7 * s, top, 4 * s, sea - top); ctx.fillRect(x + 3 * s, top, 4 * s, sea - top);
        for (const f of [0.08, 0.3, 0.55]) ctx.fillRect(x - 7 * s, top + (sea - top) * f, 14 * s, 3 * s);
      }
      // Karl the fog, rolling in through the Gate
      const drift = Math.sin(t / 1e6 * 0.25) * 18 * s;
      cloud(ctx, X(0.05) + drift, sea - Y(0.12), 26 * s, 0.75);
      cloud(ctx, X(0.17) + drift * 0.6, sea - Y(0.07), 22 * s, 0.6);
      cloud(ctx, X(0.30) - drift * 0.5, sea - Y(0.15), 18 * s, 0.5);
    }
    function campanile(ctx, t) {
      const x = X(towerX), base = towerBase, topY = base - towerH, wide = 13 * s;
      ctx.fillStyle = "#efe8d6"; ctx.strokeStyle = "rgba(90,80,60,.45)"; ctx.lineWidth = 1;
      ctx.fillRect(x - wide, topY, 2 * wide, towerH); ctx.strokeRect(x - wide, topY, 2 * wide, towerH);
      // vertical grooves
      ctx.globalAlpha = 0.25; ctx.fillStyle = "#8a7f66";
      for (const dx of [-0.5, 0, 0.5]) ctx.fillRect(x + dx * wide - 0.5, topY + 26 * s, 1, towerH - 30 * s);
      ctx.globalAlpha = 1;
      // belfry, with its arches
      ctx.fillStyle = "#e3dac3"; ctx.fillRect(x - wide - 2 * s, topY, 2 * wide + 4 * s, 24 * s);
      ctx.fillStyle = "#6b6252";
      for (const dx of [-0.55, 0, 0.55]) { ctx.beginPath(); ctx.roundRect(x + dx * wide - 3 * s, topY + 6 * s, 6 * s, 14 * s, [3 * s, 3 * s, 0, 0]); ctx.fill(); }
      // clock
      ctx.fillStyle = "#fffaf0"; ctx.beginPath(); ctx.arc(x, topY + 34 * s, 5 * s, 0, 7); ctx.fill();
      ctx.strokeStyle = "#6b6252"; ctx.lineWidth = 1; ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x, topY + 34 * s); ctx.lineTo(x, topY + 30.5 * s); ctx.moveTo(x, topY + 34 * s); ctx.lineTo(x + 2.5 * s, topY + 34 * s); ctx.stroke();
      // verdigris pyramid roof and lantern
      ctx.fillStyle = "#79a892";
      ctx.beginPath(); ctx.moveTo(x - wide - 3 * s, topY); ctx.lineTo(x, topY - 26 * s); ctx.lineTo(x + wide + 3 * s, topY); ctx.closePath(); ctx.fill();
      // the gateway's antenna, blinking
      const [ax, ay] = pos[sw];
      ctx.strokeStyle = "#555"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(x, topY - 26 * s); ctx.lineTo(ax, ay); ctx.stroke();
      ctx.fillStyle = Math.floor(t / 5e5) % 2 ? "#ff5a5a" : "#ffb3b3";
      ctx.beginPath(); ctx.arc(ax, ay, 3.5 * s + 1, 0, 7); ctx.fill();
    }
    function station(ctx, name) {
      const [x, y] = pos[name], k = s * 1.1;
      ctx.fillStyle = "#f6f1e7"; ctx.strokeStyle = "rgba(60,60,60,.5)"; ctx.lineWidth = 1;
      ctx.fillRect(x - 16 * k, y - 4 * k, 32 * k, 18 * k); ctx.strokeRect(x - 16 * k, y - 4 * k, 32 * k, 18 * k);
      ctx.fillStyle = "#c8553d";
      ctx.beginPath(); ctx.moveTo(x - 19 * k, y - 4 * k); ctx.lineTo(x, y - 16 * k); ctx.lineTo(x + 19 * k, y - 4 * k); ctx.closePath(); ctx.fill();
      ctx.fillStyle = "#7fb6d9"; ctx.fillRect(x - 11 * k, y + 1 * k, 7 * k, 6 * k); ctx.fillRect(x + 4 * k, y + 1 * k, 7 * k, 6 * k);
      // a little dish on the roof
      ctx.strokeStyle = "#666"; ctx.beginPath(); ctx.arc(x + 9 * k, y - 13 * k, 4 * k, Math.PI * 0.9, Math.PI * 1.9); ctx.stroke();
    }
    function buoy(ctx, name, i, t) {
      const [x, y] = pos[name], k = s * 1.6;
      const bob = Math.sin(t / 1e6 * 1.6 + i * 1.3) * 2.2 * s;
      const water = y + 40 * s + bob;
      ctx.save(); ctx.translate(0, bob);
      // ripple
      ctx.strokeStyle = "rgba(255,255,255,.55)"; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.ellipse(x, water - bob + 1, 17 * k, 4 * k, 0, 0, 7); ctx.stroke();
      // body: red with a white band, half under the water
      ctx.fillStyle = "#e0483c";
      ctx.beginPath(); ctx.moveTo(x - 11 * k, water - 2 * k); ctx.lineTo(x - 6 * k, water - 18 * k); ctx.lineTo(x + 6 * k, water - 18 * k);
      ctx.lineTo(x + 11 * k, water - 2 * k); ctx.quadraticCurveTo(x, water + 5 * k, x - 11 * k, water - 2 * k); ctx.fill();
      ctx.fillStyle = "#fff"; ctx.fillRect(x - 8.5 * k, water - 12 * k, 17 * k, 4 * k);
      // mast and light
      ctx.strokeStyle = "#444"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(x, water - 18 * k); ctx.lineTo(x, y + 2); ctx.stroke();
      ctx.fillStyle = "#ffd23f"; ctx.beginPath(); ctx.arc(x, y, 4 * s + 1, 0, 7); ctx.fill();
      ctx.restore();
    }
    function backdrop(ctx, t) {
      // sky and sun
      const sky = ctx.createLinearGradient(0, 0, 0, sea);
      sky.addColorStop(0, "#8ccdf2"); sky.addColorStop(1, "#fde9c9");
      ctx.fillStyle = sky; ctx.fillRect(0, 0, w, sea);
      ctx.fillStyle = "rgba(255,214,102,.9)"; ctx.beginPath(); ctx.arc(X(0.55), Y(0.14), 22 * s, 0, 7); ctx.fill();
      cloud(ctx, X(0.42) + Math.sin(t / 1e6 * 0.1) * 10 * s, Y(0.10), 14 * s, 0.85);
      cloud(ctx, X(0.68), Y(0.17), 11 * s, 0.75);
      goldenGate(ctx, t);
      // water
      const water = ctx.createLinearGradient(0, sea, 0, h);
      water.addColorStop(0, "#7cc3ea"); water.addColorStop(1, "#2f7fbf");
      ctx.fillStyle = water; ctx.fillRect(0, sea, w, h - sea);
      // waves: rows of little arcs, drifting
      ctx.strokeStyle = "rgba(255,255,255,.45)"; ctx.lineWidth = 1.2;
      for (let row = 0; row < 7; row++) {
        const y = sea + 14 * s + row * (h - sea) / 7, step = 26 * s + row * 6 * s;
        const shift = ((t / 1e6) * (8 + row * 3) * s + row * 37) % step;
        for (let x = -step + shift; x < w; x += step) {
          ctx.beginPath(); ctx.arc(x, y, 5 * s + row * 0.8 * s, Math.PI * 1.15, Math.PI * 1.85); ctx.stroke();
        }
      }
      // a sailboat crossing slowly
      const bx = ((t / 1e6) * 6 * s + X(0.1)) % (w + 60) - 30, by = Y(0.56);
      ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.moveTo(bx, by - 26 * s); ctx.lineTo(bx, by - 3 * s); ctx.lineTo(bx + 16 * s, by - 3 * s); ctx.closePath(); ctx.fill();
      ctx.fillStyle = "#33475b"; ctx.beginPath(); ctx.moveTo(bx - 10 * s, by - 2 * s); ctx.lineTo(bx + 20 * s, by - 2 * s); ctx.lineTo(bx + 15 * s, by + 3 * s); ctx.lineTo(bx - 6 * s, by + 3 * s); ctx.fill();
      // Berkeley hills, the Campanile on them, the lab beyond it
      ctx.fillStyle = "#a8c98d";
      ctx.beginPath(); ctx.moveTo(X(hillX - 0.02), sea + 2);
      for (let f = hillX; f <= 1.0001; f += 0.01) ctx.lineTo(X(f), hill(f) + Math.sin(f * 40) * 2 * s);
      ctx.lineTo(w, sea + 2); ctx.closePath(); ctx.fill();
      ctx.fillStyle = "rgba(70,120,70,.35)";
      for (const f of [0.72, 0.77, 0.9, 0.95]) ellipse(ctx, X(f), hill(f) + 6 * s, 8 * s, 6 * s);
      if (sw) campanile(ctx, t);
      shore.forEach(n => station(ctx, n));
      buoys.forEach((n, i) => buoy(ctx, n, i, t));
    }
    return { pos, path, backdrop, light: true, ownsMarker: n => own.has(n.name),
      labelGap: n => buoys.includes(n.name) ? 34 * s : shore.includes(n.name) ? 4 * s : 0,
      labelBelow: n => n.kind === "host",
      dash: l => (nodes[l.a].role === "sender" || nodes[l.b].role === "sender") ? [5, 5] : [],
      label: n => n.role === "sender" ? n.name + " · buoy" : n.kind === "switch" ? "Campanile" :
        n.name + " · lab",
      note: "Buoys reach the lab through one radio gateway, on the Campanile." };
  },

  // Anything else: switches across the middle, senders above, receivers below.
  plain(w, h) {
    const pos = {};
    switches.forEach((s, i) => {
      const x = w * (i + 1) / (switches.length + 1);
      pos[s.name] = [x, h / 2];
      const near = hostsOf(s.name);
      const top = near.filter(n => nodes[n].role !== "receiver"), bottom = near.filter(n => nodes[n].role === "receiver");
      for (const [row, y] of [[top, h * 0.17], [bottom, h * 0.83]])
        row.forEach((n, k) => { pos[n] = [x + (k - (row.length - 1) / 2) * 90, y]; });
    });
    hosts.forEach((n, i) => { if (!pos[n.name]) pos[n.name] = [40, 40 + 40 * i]; });
    return { pos, path: (a, b) => [pos[a], pos[b]],
      backdrop(ctx) { ctx.fillStyle = css("--card"); ctx.fillRect(0, 0, w, h); },
      label: n => n.name, note: "" };
  },
};

// ---- geometry -------------------------------------------------------------

let scene, routes, W = 0, H = 0;
// For each link direction: the points, and the length along them.
function measure(p) {
  const len = [0];
  for (let i = 1; i < p.length; i++) len.push(len[i - 1] + Math.hypot(p[i][0] - p[i - 1][0], p[i][1] - p[i - 1][1]));
  return { p, len };
}
function route(t) {
  routes = D.links.map(l => {
    const pts = scene.path(l.a, l.b, t);
    return [measure(pts), measure([...pts].reverse())];
  });
}
function build(w, h) {
  W = w; H = h;
  scene = (scenes[D.view] || scenes.plain)(w, h);
  document.getElementById("note").textContent = scene.note;
  route(0);
}
// A point a fraction f of the way along a route, nudged sideways so the two
// directions of a link travel in separate lanes.
function along(r, f, lane) {
  const total = r.len[r.len.length - 1], at = f * total;
  let i = 1;
  while (i < r.len.length - 1 && r.len[i] < at) i++;
  const [x0, y0] = r.p[i - 1], [x1, y1] = r.p[i];
  const seg = r.len[i] - r.len[i - 1] || 1, g = (at - r.len[i - 1]) / seg;
  const nx = -(y1 - y0) / seg, ny = (x1 - x0) / seg;
  return [x0 + (x1 - x0) * g + nx * lane, y0 + (y1 - y0) * g + ny * lane];
}

// ---- drawing --------------------------------------------------------------

// Where a link direction's queue gauge goes, as fractions along its route
// from the sending end. Just past the sending end, unless the scene says.
function gaugeSpan(r) {
  if (scene.gaugeSpan) { const span = scene.gaugeSpan(r); if (span) return span; }
  const total = r.len[r.len.length - 1];
  const start = Math.min(0.45, 22 / total);
  return [start, Math.min(0.5, start + 70 / total)];
}
const gaugeLane = () => scene.gaugeLane !== undefined ? scene.gaugeLane : 8;

// A stroke along a route from fraction a to b, following its bends, so a
// gauge on a cable that turns a corner turns with it.
function gauge(r, a, b) {
  const ctx = net.getContext("2d"), steps = 12;
  ctx.beginPath();
  for (let k = 0; k <= steps; k++) {
    const [x, y] = along(r, a + ((b - a) * k) / steps, gaugeLane());
    k ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();
}

function fit(canvas) {
  const r = devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * r) || canvas.height !== Math.round(h * r)) {
    canvas.width = Math.round(w * r); canvas.height = Math.round(h * r);
  }
  const ctx = canvas.getContext("2d"); ctx.setTransform(r, 0, 0, r, 0, 0);
  return [ctx, w, h];
}

let now = 0, playing = false, last = null, speed = 0.1;
const net = document.getElementById("net");

function drawNet() {
  const [ctx, w, h] = fit(net);
  if (w !== W || h !== H) build(w, h);
  // Satellites move, so their links do too.
  if (scene.moving) route(now);
  const hidden = i => scene.hidden && scene.hidden(D.links[i], now);
  ctx.clearRect(0, 0, w, h);
  scene.backdrop(ctx, now);
  const onDark = scene.dark, ink = onDark ? "#e8ecf5" : scene.light ? "#1d232b" : css("--node");

  // links
  D.links.forEach((l, i) => {
    if (hidden(i)) return;
    const r = routes[i][0], black = isDark(i, now);
    ctx.strokeStyle = black ? css("--dark") : scene.linkColor ? scene.linkColor(l)
      : onDark ? "rgba(200,215,255,.45)" : scene.light ? "rgba(40,60,80,.45)" : css("--line");
    ctx.lineWidth = black ? 3 : 2.5;
    ctx.setLineDash(black ? [6, 5] : scene.dash ? scene.dash(l) : []);
    ctx.beginPath(); r.p.forEach(([x, y], k) => k ? ctx.lineTo(x, y) : ctx.moveTo(x, y)); ctx.stroke();
    ctx.setLineDash([]);
    if (black) {
      const [x, y] = along(r, 0.3, -16);
      ctx.fillStyle = onDark ? "#fda4af" : css("--dark"); ctx.font = "600 12px sans-serif"; ctx.textAlign = "center";
      ctx.fillText("blackout", x, y);
    }
  });

  // queues: a gauge just after the sending end, filling toward full
  D.links.forEach((l, i) => {
    if (hidden(i)) return;
    for (const w2 of [0, 1]) {
      const q = queueAt(2 * i + w2, now);
      if (q < 1) continue;
      const r = routes[i][w2], frac = Math.min(1, q / l.queue[w2]);
      const [start, stop] = gaugeSpan(r);
      ctx.strokeStyle = css("--queue"); ctx.lineWidth = 5; ctx.lineCap = "round";
      ctx.globalAlpha = 0.25;
      gauge(r, start, stop);
      ctx.globalAlpha = 1;
      gauge(r, start, start + (stop - start) * frac);
      ctx.lineCap = "butt";
    }
  });

  // packets in flight
  const from = firstTxAfter(now - maxFlight), to = firstTxAfter(now + 1);
  for (let k = from; k < to; k++) {
    const i = order[k], d = D.tx[4 * i], s = D.tx[4 * i + 1], e = D.tx[4 * i + 2], size = D.tx[4 * i + 3];
    if (now < s || now > e) continue;
    const big = size > 200;
    const [x, y] = along(routes[d >> 1][d & 1], e > s ? (now - s) / (e - s) : 1, 4);
    ctx.fillStyle = big ? css("--data") : css("--ack");
    ctx.beginPath(); ctx.arc(x, y, big ? 3.6 : 2.6, 0, 7); ctx.fill();
  }

  // drops in a short window before now, fading out
  const window_ = Math.max(30000, speed * 400000);
  for (let k = dropsBefore(now) - 1; k >= 0 && dropTimes[k][0] > now - window_; k--) {
    const [t, d] = dropTimes[k], r = routes[d >> 1][d & 1];
    const [x, y] = along(r, gaugeSpan(r)[0], gaugeLane());
    const fade = 1 - (now - t) / window_;
    ctx.globalAlpha = fade; ctx.strokeStyle = css("--drop"); ctx.lineWidth = 2;
    const s = 4 + 4 * (1 - fade);
    ctx.beginPath(); ctx.moveTo(x - s, y - s); ctx.lineTo(x + s, y + s); ctx.moveTo(x + s, y - s); ctx.lineTo(x - s, y + s); ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // nodes and labels
  for (const n of D.nodes) {
    // A scene can draw a node itself (a satellite) by leaving it out of pos.
    const p = scene.pos[n.name];
    if (!p) continue;
    const [x, y] = p;
    ctx.fillStyle = onDark ? "#0b1530" : css("--card");
    ctx.strokeStyle = ink; ctx.lineWidth = 2;
    if (scene.ownsMarker && scene.ownsMarker(n)) {
      // drawn by the scene's backdrop
    } else if (n.kind === "switch") {
      ctx.beginPath(); ctx.roundRect(x - 13, y - 10, 26, 20, 5); ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x - 6, y - 2); ctx.lineTo(x + 6, y - 2); ctx.moveTo(x - 6, y + 3); ctx.lineTo(x + 6, y + 3); ctx.stroke();
    } else {
      ctx.beginPath(); ctx.arc(x, y, 8, 0, 7); ctx.fill(); ctx.stroke();
      ctx.fillStyle = n.role === "sender" ? css("--data") : n.role === "receiver" ? css("--ack") : css("--muted");
      ctx.beginPath(); ctx.arc(x, y, 3.5, 0, 7); ctx.fill();
    }
    ctx.fillStyle = ink; ctx.font = "600 12px sans-serif"; ctx.textAlign = "center";
    if (scene.ownsLabel && scene.ownsLabel(n)) continue;
    const below = scene.labelBelow ? scene.labelBelow(n) : n.kind === "host" && y > h * 0.5;
    // A scene that draws its own, taller, node can push the label clear of it.
    const gap = scene.labelGap ? scene.labelGap(n) : 0;
    const ly = below ? y + 24 + gap : y - 16 - gap;
    ctx.fillText((D.labels || {})[n.name] || scene.label(n), x, ly);
    if (n.ip) { ctx.font = "11px sans-serif"; ctx.globalAlpha = 0.65; ctx.fillText(n.ip, x, below ? ly + 14 : ly - 14); ctx.globalAlpha = 1; }
  }
}

// ---- charts ---------------------------------------------------------------

function chart(id, keysId, series) {
  const canvas = document.getElementById(id), [ctx, w, h] = fit(canvas);
  ctx.clearRect(0, 0, w, h);
  const left = 40, right = 12, top = 10, bottom = 22, pw = w - left - right, ph = h - top - bottom;
  let max = 1;
  for (const s of series) for (const [, v] of s.points) max = Math.max(max, v);
  const X = t => left + (t / END) * pw, Y = v => top + ph - (v / max) * ph;
  ctx.strokeStyle = css("--line"); ctx.lineWidth = 1; ctx.fillStyle = css("--muted");
  ctx.font = "11px sans-serif"; ctx.textAlign = "right";
  for (let g = 0; g <= 4; g++) {
    const v = (max * g) / 4, y = Y(v);
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + pw, y); ctx.stroke();
    ctx.fillText(v >= 10 ? v.toFixed(0) : v.toFixed(1), left - 6, y + 4);
  }
  for (let g = 0; g <= 5; g++) {
    ctx.textAlign = g === 0 ? "left" : g === 5 ? "right" : "center";
    ctx.fillText(((END * g) / 5 / 1e6).toFixed(1) + " s", X((END * g) / 5), h - 6);
  }
  // blackouts shaded
  ctx.fillStyle = css("--dark"); ctx.globalAlpha = 0.08;
  for (const spans of Object.values(dark)) for (const [a, b] of spans) ctx.fillRect(X(a), top, X(b) - X(a), ph);
  ctx.globalAlpha = 1;
  series.forEach((s, i) => {
    ctx.strokeStyle = SERIES[i % SERIES.length]; ctx.lineWidth = 1.6;
    ctx.beginPath(); s.points.forEach(([t, v], j) => j ? ctx.lineTo(X(t), Y(v)) : ctx.moveTo(X(t), Y(v))); ctx.stroke();
  });
  ctx.strokeStyle = css("--fg"); ctx.globalAlpha = 0.6;
  ctx.beginPath(); ctx.moveTo(X(now), top); ctx.lineTo(X(now), top + ph); ctx.stroke();
  ctx.globalAlpha = 1;
  document.getElementById(keysId).innerHTML = series.length ? series.map((s, i) =>
    `<span><i style="background:${SERIES[i % SERIES.length]}"></i>${s.name}</span>`).join("") : "<span>nothing to show</span>";
}

const qSeries = Object.entries(D.queues)
  .filter(([, pts]) => pts.some(([, n]) => n > 1))
  .map(([d, pts]) => {
    const l = D.links[d >> 1], show = n => (D.labels || {})[n] || n;
    const name = d & 1 ? `${show(l.b)} → ${show(l.a)}` : `${show(l.a)} → ${show(l.b)}`;
    const steps = [];
    for (const [t, n] of pts) { if (steps.length) steps.push([t, steps[steps.length - 1][1]]); steps.push([t, n]); }
    return { name, points: steps };
  });
// Throughput comes in Mbps; show it in whatever unit fits this trial.
const tMax = Math.max(0, ...Object.values(D.throughput).flat());
const [tScale, tUnit] = tMax >= 1 ? [1, "Mbps"] : tMax >= 0.001 ? [1e3, "kbps"] : [1e6, "bps"];
document.getElementById("ttitle").textContent = "Delivered throughput, " + tUnit;
const tSeries = Object.entries(D.throughput).map(([name, buckets]) => ({
  name, points: buckets.map((v, i) => [i * D.throughput_bucket_us, v * tScale]) }));

// ---- controls -------------------------------------------------------------

const ICON_PLAY = '<svg viewBox="0 0 10 10"><path d="M2 1l7 4-7 4z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 10 10"><path d="M2 1h2v8H2zM6 1h2v8H6z"/></svg>';
const play = document.getElementById("play");
play.innerHTML = ICON_PLAY;
const SPEEDS = [[0.001, "1/1000×"], [0.01, "1/100×"], [0.1, "1/10×"], [1, "1×"], [10, "10×"]];
const speedBox = document.getElementById("speeds");
for (const [v, label] of SPEEDS) {
  const b = document.createElement("button");
  b.textContent = label; b.className = v === speed ? "on" : "";
  b.onclick = () => { speed = v; [...speedBox.children].forEach(c => c.className = ""); b.className = "on"; };
  speedBox.appendChild(b);
}
document.getElementById("bands").innerHTML = Object.values(dark).flat().map(([a, b]) =>
  `<i style="left:${(a / END) * 100}%;width:${((b - a) / END) * 100}%"></i>`).join("");
const scrub = document.getElementById("scrub");

function draw() {
  drawNet();
  chart("queues", "qkeys", qSeries);
  chart("tput", "tkeys", tSeries);
  const dropped = dropsBefore(now);
  document.getElementById("clock").innerHTML =
    `<b>${(now / 1e6).toFixed(4)} s</b> / ${(END / 1e6).toFixed(1)} s · ${dropped} dropped`;
  scrub.value = Math.round((now / END) * 1000);
}
function tick(ts) {
  if (!playing) return;
  if (last !== null) now = Math.min(END, now + (ts - last) * 1000 * speed);
  last = ts;
  draw();
  if (now >= END) { playing = false; play.innerHTML = ICON_PLAY; return; }
  requestAnimationFrame(tick);
}
play.onclick = () => {
  playing = !playing; last = null;
  if (playing && now >= END) now = 0;
  play.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
  play.setAttribute("aria-label", playing ? "Pause" : "Play");
  if (playing) requestAnimationFrame(tick);
};
scrub.oninput = e => { now = (e.target.value / 1000) * END; draw(); };
addEventListener("keydown", e => { if (e.code === "Space") { e.preventDefault(); play.click(); } });
addEventListener("resize", draw);
// vis.html#t=2.5 opens at 2.5 s of simulated time.
const start = /t=([0-9.]+)/.exec(location.hash);
if (start) now = Math.min(END, parseFloat(start[1]) * 1e6);
draw();
</script>
</body>
</html>
"""
