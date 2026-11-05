"""Scenario discovery.

A scenario is a directory, not a code change. `discover()` scans `scenarios/`
plus every path in TRANSPORTLAB_SCENARIO_PATH.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILTIN_SCENARIOS = REPO_ROOT / "scenarios"


class ScenarioError(ValueError):
    pass


@dataclass
class Claims:
    """How wire numbers map onto the numbers the scenario says it models.

    By default the datacenter scenario runs 100 Mbps at a 100 ms RTT in place
    of 10 Gbps at 1 ms. That keeps the bandwidth-delay
    product and the cost of a loss in round trips intact, which is what the
    protocol design actually turns on. These multipliers let the report state
    both the wire number and the number it stands in for.
    """

    throughput_multiplier: float = 1.0   # wire bps * this = claimed bps
    latency_multiplier: float = 1.0      # wire ms * this = claimed ms
    time_factor: float = 1.0             # claimed seconds per wire second
    note: str = ""

    @classmethod
    def from_manifest(cls, d: dict | None) -> "Claims":
        d = dict(d or {})
        unknown = set(d) - {"throughput_multiplier", "latency_multiplier", "time_factor", "note"}
        if unknown:
            raise ScenarioError(f"unknown keys in claims: {sorted(unknown)}")
        return cls(
            throughput_multiplier=float(d.get("throughput_multiplier", 1.0)),
            latency_multiplier=float(d.get("latency_multiplier", 1.0)),
            time_factor=float(d.get("time_factor", 1.0)),
            note=str(d.get("note", "")),
        )

    def to_dict(self) -> dict:
        return {
            "throughput_multiplier": self.throughput_multiplier,
            "latency_multiplier": self.latency_multiplier,
            "time_factor": self.time_factor,
            "note": self.note,
        }


@dataclass
class Scenario:
    id: str
    title: str
    path: Path
    manifest: dict
    claims: Claims
    hosts: dict[str, str] = field(default_factory=dict)   # host name -> role
    # "scaled" or "realistic". See _apply_parameters.
    parameters: str = "scaled"
    # Wire-unit card times are divided by this. 1 except in a realistic
    # profile that speeds the network up, so the same card keeps its shape.
    wire_time_divisor: float = 1.0

    def card_divisor(self, card: dict) -> float:
        """What a card's times are divided by to get simulated seconds."""
        if card.get("time_unit") == "claimed":
            return self.claims.time_factor
        return self.wire_time_divisor

    @property
    def requirements(self) -> dict:
        return self.manifest.get("requirements", {})

    @property
    def topology(self) -> dict:
        return self.manifest["topology"]

    @property
    def switches(self) -> list[str]:
        return list(self.topology.get("switches", []))

    def host_names(self, role: str | None = None) -> list[str]:
        if role is None:
            return list(self.hosts)
        return [h for h, r in self.hosts.items() if r == role]

    def workloads(self) -> dict[str, Path]:
        """Cards in the scenario dir, plus any on TRANSPORTLAB_WORKLOAD_PATH."""
        found: dict[str, Path] = {}
        for card in sorted((self.path / "workloads").glob("*.yaml")):
            found[card.stem] = card
        for root in _overlay_workload_roots():
            for card in sorted((root / self.id).glob("*.yaml")):
                found[card.stem] = card
        return found

    def workload_path(self, name: str) -> Path:
        """Resolve a workload by name, or by path to a card you wrote.

        A path to a card you wrote is as valid as a name.
        """
        as_path = Path(name).expanduser()
        if as_path.suffix in (".yaml", ".yml") or as_path.is_file():
            if not as_path.is_file():
                raise ScenarioError(f"no workload card at {as_path}")
            return as_path
        cards = self.workloads()
        if name not in cards:
            raise ScenarioError(
                f"scenario {self.id} has no workload {name!r}. available: "
                f"{sorted(cards)}, or pass a path to a card you wrote"
            )
        return cards[name]

    def scoring(self) -> dict:
        f = self.path / "scoring.yaml"
        return yaml.safe_load(f.read_text()) if f.exists() else {}


def _paths_from_env(var: str) -> list[Path]:
    raw = os.environ.get(var, "")
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


def _scenario_roots() -> list[Path]:
    roots = [BUILTIN_SCENARIOS, *_paths_from_env("TRANSPORTLAB_SCENARIO_PATH")]
    return [r for r in roots if r.is_dir()]


def _overlay_workload_roots() -> list[Path]:
    return [r for r in _paths_from_env("TRANSPORTLAB_WORKLOAD_PATH") if r.is_dir()]


def _host_names(topology: dict) -> dict[str, str]:
    """Turn `hosts: {senders: 8}` into sender0..sender7, each tagged with its role."""
    spec = topology.get("hosts")
    if not isinstance(spec, dict) or not spec:
        raise ScenarioError("topology.hosts must be a non-empty mapping of role to count")
    names: dict[str, str] = {}
    for role, count in spec.items():
        prefix = role[:-1] if role.endswith("s") else role
        n = int(count)
        if n < 1:
            raise ScenarioError(f"role {role} needs at least one host")
        for i in range(n):
            names[f"{prefix}{i}"] = role
    return names


def load_manifest(path: Path) -> dict:
    manifest = yaml.safe_load((path / "scenario.yaml").read_text())
    if not isinstance(manifest, dict):
        raise ScenarioError(f"{path}/scenario.yaml is not a yaml mapping")
    for key in ("id", "title", "topology"):
        if key not in manifest:
            raise ScenarioError(f"{path}/scenario.yaml is missing {key!r}")
    if not manifest["topology"].get("links"):
        raise ScenarioError(f"{path}/scenario.yaml: topology.links is empty")
    return manifest


PARAMETERS = ("scaled", "realistic")


def _apply_parameters(manifest: dict, path: Path) -> tuple[dict, str, float]:
    """Pick the scaled or the realistic version of a scenario's network.

    A scenario that scales its network (datacenter, sensor) can also carry a
    `realistic:` block with the real link values and claims. `parameters:`
    in scenario.yaml picks one; TRANSPORTLAB_PARAMETERS overrides it for every
    scenario that has a realistic block. Requirements
    are in claimed units, so they apply to both.
    """
    chosen = os.environ.get("TRANSPORTLAB_PARAMETERS") or manifest.get("parameters", "scaled")
    if chosen not in PARAMETERS:
        raise ScenarioError(f"{path}: parameters must be one of {PARAMETERS}, not {chosen!r}")
    block = manifest.get("realistic")
    if chosen == "scaled" or not block:
        return manifest, "scaled", 1.0
    manifest = dict(manifest)
    manifest["claims"] = block.get("claims", {})
    manifest["topology"] = {**manifest["topology"], "links": block["links"]}
    return manifest, "realistic", float(block.get("wire_time_divisor", 1.0))


def from_dir(path: Path) -> Scenario:
    manifest = load_manifest(path)
    manifest, parameters, wire_time_divisor = _apply_parameters(manifest, path)
    return Scenario(
        id=str(manifest["id"]),
        title=str(manifest["title"]),
        path=path,
        manifest=manifest,
        claims=Claims.from_manifest(manifest.get("claims")),
        hosts=_host_names(manifest["topology"]),
        parameters=parameters,
        wire_time_divisor=wire_time_divisor,
    )


def discover() -> dict[str, Scenario]:
    found: dict[str, Scenario] = {}
    for root in _scenario_roots():
        for child in sorted(root.iterdir()):
            if (child / "scenario.yaml").is_file():
                scenario = from_dir(child)
                found[scenario.id] = scenario
    return found


def load(scenario_id: str) -> Scenario:
    found = discover()
    if scenario_id not in found:
        raise ScenarioError(
            f"unknown scenario {scenario_id!r}. available: {sorted(found)}"
        )
    return found[scenario_id]
