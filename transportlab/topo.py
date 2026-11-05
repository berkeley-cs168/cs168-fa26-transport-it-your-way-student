"""Manifest to topology.

`normalize_links` expands a scenario's links into one spec per pair of nodes,
with the shaping for each direction. `sim_params` turns that shaping into the
units ns-3 takes. Link parameters are kept internally under the names Mininet
used (bw, delay, loss, max_queue_size), which workload events also use.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .scenario import Scenario, ScenarioError
from .trace import expand

# Manifest key -> internal key. Anything else in a link block is an error,
# so a typo like `delay_s` fails at load instead of silently shaping nothing.
_PARAM_MAP = {
    "bw_mbps": "bw",
    "delay_ms": "delay",
    "jitter_ms": "jitter",
    "loss_pct": "loss",
    "queue_pkts": "max_queue_size",
}


@dataclass
class LinkSpec:
    a: str
    b: str
    params1: dict = field(default_factory=dict)   # shapes traffic leaving a
    params2: dict = field(default_factory=dict)   # shapes traffic leaving b

    @property
    def name(self) -> str:
        return f"{self.a}-{self.b}"


def to_tc_params(block: dict) -> dict:
    out = {}
    for key, value in block.items():
        if key in ("a", "b", "a_to_b", "b_to_a"):
            continue
        if key not in _PARAM_MAP:
            raise ScenarioError(
                f"unknown link parameter {key!r}. known: {sorted(_PARAM_MAP)}"
            )
        tc = _PARAM_MAP[key]
        if tc in ("delay", "jitter"):
            out[tc] = f"{float(value)}ms"
        elif tc == "max_queue_size":
            out[tc] = int(value)
        else:
            out[tc] = float(value)
    return out


def normalize_links(scenario: Scenario) -> list[LinkSpec]:
    """Expand host patterns and split each link into two shaped directions."""
    names = list(scenario.hosts) + scenario.switches
    specs: list[LinkSpec] = []
    for block in scenario.topology["links"]:
        if "a" not in block or "b" not in block:
            raise ScenarioError(f"link {block} needs both a and b")
        base = to_tc_params(block)
        forward = {**base, **to_tc_params(block.get("a_to_b", {}))}
        reverse = {**base, **to_tc_params(block.get("b_to_a", {}))}
        for a in expand(block["a"], names):
            for b in expand(block["b"], names):
                if a == b:
                    continue
                specs.append(LinkSpec(a=a, b=b, params1=dict(forward), params2=dict(reverse)))
    if not specs:
        raise ScenarioError(f"scenario {scenario.id} produced no links")
    return specs


def check_connectivity(scenario: Scenario, specs: list[LinkSpec]) -> None:
    """Every host has to be reachable, otherwise the trial fails in a confusing way."""
    adjacency: dict[str, set[str]] = {}
    for s in specs:
        adjacency.setdefault(s.a, set()).add(s.b)
        adjacency.setdefault(s.b, set()).add(s.a)
    hosts = list(scenario.hosts)
    seen = {hosts[0]}
    queue = [hosts[0]]
    while queue:
        node = queue.pop()
        for peer in adjacency.get(node, ()):
            if peer not in seen:
                seen.add(peer)
                queue.append(peer)
    missing = [h for h in hosts if h not in seen]
    if missing:
        raise ScenarioError(f"hosts unreachable in scenario {scenario.id}: {missing}")


# A link with no bw_mbps is unshaped: 100 Gbps, so serialization is
# effectively free but still deterministic.
UNSHAPED_BPS = 100_000_000_000

# Packets a queue holds when the manifest gives no queue_pkts. Same as
# netem's default limit, which is what Mininet used.
DEFAULT_QUEUE_PKTS = 1000


def _ms_to_ns(value) -> int:
    return round(float(str(value).removesuffix("ms")) * 1_000_000)


def sim_params(params: dict) -> dict:
    """One direction of a link, in the integer units the simulator takes."""
    bw = params.get("bw")
    return {
        "bps": round(float(bw) * 1_000_000) if bw else UNSHAPED_BPS,
        "delay_ns": _ms_to_ns(params.get("delay", 0)),
        "jitter_ns": _ms_to_ns(params.get("jitter", 0)),
        "loss": float(params.get("loss", 0.0)) / 100.0,
        "queue_pkts": int(params.get("max_queue_size", DEFAULT_QUEUE_PKTS)),
    }
