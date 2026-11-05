"""Satellite passes, from a scenario's `constellation:` block.

LEO satellites cross the sky in a few minutes, and a terminal is handed from
one to the next on a fixed schedule (Starlink reassigns every 15 s). Each
satellite here is a switch between `ground` and `terminal`. It is in view for
`pass_s`, then goes out of view; the next satellite comes into view
`handover_ms` later. While none is in view, nothing gets through. The
simulator routes through whichever satellite is in view, and a satellite
going out of view drops what it has queued and in flight.

While a satellite is in view its links get an extra delay for the slant
range: the shortest at the middle of the pass, when it is overhead, and the
longest at the start and end, when it is near the horizon.

    constellation:
      satellites: "sat*"        # switch names, in the order they serve
      pass_s: 5.0               # each one is in view this long
      handover_ms: 300          # then nothing, until the next one rises
      phase_s: 0.0              # how far into its pass sat0 is at t = 0
      slant_delay_ms: {overhead: 1.8, horizon: 3.7}   # added to each hop
      step_s: 0.25              # how often the slant delay is updated

All times are wire seconds. The schedule is the same for every card and every
seed: orbits don't depend on the traffic.
"""

from __future__ import annotations

import fnmatch
import math
from dataclasses import dataclass

from .scenario import Scenario, ScenarioError

_KEYS = {"satellites", "pass_s", "handover_ms", "phase_s", "slant_delay_ms", "step_s"}


@dataclass
class Pass:
    satellite: str
    rise_s: float     # comes into view
    set_s: float      # goes out of view


@dataclass
class Constellation:
    satellites: list[str]
    pass_s: float
    handover_s: float
    phase_s: float
    overhead_ms: float
    horizon_ms: float
    step_s: float

    def passes(self, until_s: float) -> list[Pass]:
        """Every pass that overlaps [0, until_s], in time order."""
        period = self.pass_s + self.handover_s
        out = []
        k = 0
        while True:
            rise = k * period - self.phase_s
            if rise > until_s:
                return out
            sat = self.satellites[k % len(self.satellites)]
            out.append(Pass(sat, rise, rise + self.pass_s))
            k += 1

    def slant_ms(self, p: Pass, t_s: float) -> float:
        """Extra one-hop delay at t_s, inside pass p."""
        f = min(1.0, max(0.0, (t_s - p.rise_s) / self.pass_s))
        # 0 overhead, 1 at either horizon.
        low = (2 * f - 1) ** 2
        return self.overhead_ms + (self.horizon_ms - self.overhead_ms) * low


def load(scenario: Scenario) -> Constellation | None:
    block = scenario.manifest.get("constellation")
    if not block:
        return None
    unknown = set(block) - _KEYS
    if unknown:
        raise ScenarioError(f"unknown keys in constellation: {sorted(unknown)}")
    pattern = block.get("satellites", "sat*")
    patterns = [pattern] if isinstance(pattern, str) else list(pattern)
    sats = [s for p in patterns for s in scenario.switches if fnmatch.fnmatchcase(s, p)]
    if not sats:
        raise ScenarioError(f"constellation satellites {pattern!r} match no switch")
    slant = block.get("slant_delay_ms", {})
    c = Constellation(
        satellites=sats,
        pass_s=float(block.get("pass_s", 5.0)),
        handover_s=float(block.get("handover_ms", 300)) / 1000.0,
        phase_s=float(block.get("phase_s", 0.0)),
        overhead_ms=float(slant.get("overhead", 0.0)),
        horizon_ms=float(slant.get("horizon", 0.0)),
        step_s=float(block.get("step_s", 0.25)),
    )
    if c.pass_s <= 0 or c.handover_s < 0 or c.step_s <= 0:
        raise ScenarioError("constellation needs pass_s > 0, handover_ms >= 0, step_s > 0")
    if not 0 <= c.phase_s < c.pass_s:
        raise ScenarioError("constellation phase_s must be in [0, pass_s)")
    return c


def satellite_links(c: Constellation, link_names: list[str]) -> dict[str, list[str]]:
    """The links that touch each satellite."""
    out = {}
    for sat in c.satellites:
        out[sat] = [n for n in link_names if sat in n.split("-")]
    return out


def states(c: Constellation, until_s: float, fixed: bool = False) -> list[tuple[float, str, dict, str]]:
    """What each satellite looks like over time, as (t_s, satellite, state, label).

    A state is {"out_of_view": True} or {"extra_delay_ms": x}; each one
    replaces the satellite's previous state. Labels: `rise`, `set` and
    `slant`. With `fixed`, the first satellite stays overhead the whole trial
    and the others never rise, for the autograder's clean network.
    """
    if fixed:
        out = [(0.0, c.satellites[0], {"extra_delay_ms": c.overhead_ms}, "rise")]
        out += [(0.0, s, {"out_of_view": True}, "set") for s in c.satellites[1:]]
        return out
    out = []
    for p in c.passes(until_s):
        start = max(0.0, p.rise_s)
        out.append((start, p.satellite, {"extra_delay_ms": round(c.slant_ms(p, start), 4)}, "rise"))
        # Delay steps on a fixed grid from the rise, so they don't drift with
        # the phase.
        n = math.ceil((start - p.rise_s) / c.step_s - 1e-9)
        while True:
            n += 1
            t = p.rise_s + n * c.step_s
            if t >= p.set_s - 1e-9 or t > until_s:
                break
            out.append((round(t, 9), p.satellite,
                        {"extra_delay_ms": round(c.slant_ms(p, t), 4)}, "slant"))
        if p.set_s <= until_s:
            out.append((round(p.set_s, 9), p.satellite, {"out_of_view": True}, "set"))
    # Satellites that haven't risen yet start out of view.
    for s in c.satellites:
        first = next((p for p in c.passes(until_s) if p.satellite == s), None)
        if first is None or first.rise_s > 0:
            out.append((0.0, s, {"out_of_view": True}, "set"))
    out.sort(key=lambda x: (x[0], x[3] != "set", x[1]))
    return out
