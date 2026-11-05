"""Mid-run network changes.

A LEO link goes dark for twenty seconds. A handover doubles the RTT. A switch
starts dropping. These are what separate a protocol that works on a quiet link
from one that survives the scenario, so they are first-class parts of a workload
card rather than something baked into a topology.

Changes stack. Each card event is a layer on its link from its start until
its `duration_s` runs out, and a satellite's pass (transportlab/orbit.py) is
one more. `link_timeline` folds the layers into what each link direction
looks like after every change:

- a blackout, or a satellite out of view, makes the link dark, whatever else
  is going on. Going dark drops what is queued and on the wire
- otherwise the latest layer to set `loss`, `delay`, `jitter` or `bw` wins,
  over the scenario's baseline
- a satellite's slant delay adds to whatever `delay` ends up being
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from .topo import LinkSpec
from .trace import WorkloadError

KINDS = ("blackout", "loss_burst", "delay_change", "bw_change")
DIRECTIONS = ("both", "forward", "reverse")


@dataclass
class LinkAction:
    t_s: float
    links: list[str]
    params: dict            # the layer's overrides; empty on the event's end
    direction: str
    label: str
    key: int = 0            # which card event this starts or ends
    end: bool = False
    applied: bool = field(default=False, compare=False)

    def describe(self) -> str:
        what = "restore" if self.end else ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.label} on {','.join(self.links)} [{self.direction}] {what}"


def _match(target: str, specs: list[LinkSpec]) -> list[str]:
    names = [s.name for s in specs]
    hit = [n for n in names if fnmatch.fnmatchcase(n, target)]
    if not hit:
        # Links are named a-b. Accept b-a too so a card does not have to know
        # which side the manifest listed first.
        flipped = "-".join(reversed(target.split("-", 1)))
        hit = [n for n in names if fnmatch.fnmatchcase(n, flipped)]
    if not hit:
        raise WorkloadError(f"event target {target!r} matched no link. links: {names}")
    return hit


def schedule_from_card(card: dict, specs: list[LinkSpec], time_divisor: float = 1.0) -> list[LinkAction]:
    """Expand a card's events into a time-ordered list of link changes."""
    actions: list[LinkAction] = []
    for key, raw in enumerate(card.get("events", []) or []):
        kind = raw.get("kind")
        if kind not in KINDS:
            raise WorkloadError(f"unknown event kind {kind!r}, expected one of {KINDS}")
        direction = raw.get("direction", "both")
        if direction not in DIRECTIONS:
            raise WorkloadError(f"event direction must be one of {DIRECTIONS}")
        if "target" not in raw:
            raise WorkloadError(f"event {raw} needs a target link")
        links = _match(str(raw["target"]), specs)
        start = float(raw.get("t_s", 0.0)) / time_divisor
        duration = raw.get("duration_s")
        duration = None if duration is None else float(duration) / time_divisor

        if kind == "blackout":
            params = {"dark": True}
        elif kind == "loss_burst":
            params = {"loss": float(raw.get("loss_pct", 1.0))}
        elif kind == "delay_change":
            params = {"delay": f"{float(raw['delay_ms'])}ms"}
            if "jitter_ms" in raw:
                params["jitter"] = f"{float(raw['jitter_ms'])}ms"
        else:
            params = {"bw": float(raw["bw_mbps"])}

        actions.append(LinkAction(start, links, params, direction, kind, key))
        if duration:
            actions.append(LinkAction(start + duration, links, {}, direction, f"{kind}_end",
                                      key, end=True))

    actions.sort(key=lambda a: (a.t_s, a.label))
    return actions


def _effective(baseline: dict, layers: dict, orbit: dict) -> dict:
    out = dict(baseline)
    dark = bool(orbit.get("out_of_view"))
    for layer in layers.values():         # in the order they started
        for k, v in layer.items():
            if k == "dark":
                dark = True
            else:
                out[k] = v
    extra = orbit.get("extra_delay_ms", 0.0)
    if extra:
        base = float(str(out.get("delay", "0ms")).removesuffix("ms"))
        out["delay"] = f"{round(base + extra, 6)}ms"
    if dark:
        out["loss"] = 100.0
    return out


def link_timeline(
    actions: list[LinkAction],
    specs: list[LinkSpec],
    orbit_states: list[tuple[float, str, dict, str]] = (),
    satellite_links: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Every change to every link direction, as the simulator applies them.

    Returns dicts with `t_s`, `link` (a name), `wire` (0 is a to b), `params`
    (the direction's full parameters after the change), `flush` (it just
    went dark) and `label` (what caused it).
    """
    baseline = {}
    for s in specs:
        baseline[(s.name, 0)] = s.params1
        baseline[(s.name, 1)] = s.params2
    layers: dict[tuple[str, int], dict[int, dict]] = {k: {} for k in baseline}
    orbit: dict[tuple[str, int], dict] = {k: {} for k in baseline}
    dark = {k: False for k in baseline}

    # One list of changes: (t, order, wires touched, how to apply, label).
    changes = []
    for i, a in enumerate(actions):
        wires = {"both": (0, 1), "forward": (0,), "reverse": (1,)}[a.direction]
        touched = [(n, w) for n in a.links for w in wires]
        changes.append((a.t_s, 1, i, touched, ("card", a), a.label))
    for i, (t, sat, state, label) in enumerate(orbit_states):
        touched = [(n, w) for n in (satellite_links or {}).get(sat, []) for w in (0, 1)]
        changes.append((t, 0, i, touched, ("orbit", state), label))
    # At the same instant, orbit before cards, then in schedule order.
    changes.sort(key=lambda c: (c[0], c[1], c[2]))

    out = []
    for t, _, _, touched, (what, payload), label in changes:
        for key in touched:
            if what == "card":
                if payload.end:
                    layers[key].pop(payload.key, None)
                else:
                    layers[key][payload.key] = payload.params
            else:
                orbit[key] = payload
            params = _effective(baseline[key], layers[key], orbit[key])
            now_dark = params.get("loss", 0.0) >= 100.0
            out.append({"t_s": t, "link": key[0], "wire": key[1], "params": params,
                        "flush": now_dark and not dark[key], "label": label})
            dark[key] = now_dark
    return out
