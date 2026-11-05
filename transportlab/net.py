"""The loopback runner, plus addressing shared with the ns-3 runner.

`loopback` runs every endpoint as a process on 127.0.0.1 with no shaping and no
root. It is wrong about the network on purpose: it exists so a student can debug
protocol logic over real sockets in two seconds. Link events are skipped there
and the results file says so. The gradeable runner is ns-3, in orchestrator.py.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .scenario import Scenario
from .topo import LinkSpec, check_connectivity, normalize_links

# Host i (in scenario order) is 10.0.0.(i+1), port BASE_PORT + i.
BASE_PORT = 15000

# Bytes captured per packet under PCAP=1. The PPP, IP and UDP headers take 30,
# so this leaves 98 bytes of payload, which is the front of whatever header the
# protocol defined. Capturing the whole 1400 byte payload would multiply the
# file size to store hash filler nobody reads.
PCAP_SNAPLEN = 128


class Runner:
    """Common interface the orchestrator drives."""

    kind = "abstract"
    shapes_traffic = False

    def __init__(self, scenario: Scenario, log_dir: Path):
        self.scenario = scenario
        self.log_dir = Path(log_dir)
        self.links: list[LinkSpec] = normalize_links(scenario)
        check_connectivity(scenario, self.links)
        self.ports = {
            host: BASE_PORT + i for i, host in enumerate(scenario.hosts)
        }
        self.warnings: list[str] = []
        # Facts about how the trial ran, recorded in results.json. Which switch
        # was used belongs in the provenance of a graded number.
        self.notes: list[str] = []

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def ip(self, host: str) -> str:
        raise NotImplementedError

    def bind_ip(self, host: str) -> str:
        return "0.0.0.0"

    def addr(self, host: str) -> tuple[str, int]:
        return self.ip(host), self.ports[host]

    def peer_table(self) -> dict[str, tuple[str, int]]:
        return {h: self.addr(h) for h in self.scenario.hosts}

    def popen(self, host: str, argv: list[str]) -> subprocess.Popen:
        raise NotImplementedError

    def apply(self, action) -> str:
        return f"skipped ({self.kind} does not shape traffic)"

    def start_capture(self, hosts: list[str]) -> list[str]:
        """Begin writing a pcap per host. Returns notes for the results file."""
        return [f"packet capture skipped, {self.kind} has no interfaces to capture"]

    def stop_capture(self) -> None:
        pass


class LoopbackRunner(Runner):
    kind = "loopback"

    def start(self) -> None:
        self.warnings.append(
            "loopback mode: no bandwidth, delay, loss, or link events. protocol logic "
            "only, never a gradeable number"
        )

    def stop(self) -> None:
        pass

    def ip(self, host: str) -> str:
        return "127.0.0.1"

    def bind_ip(self, host: str) -> str:
        return "127.0.0.1"

    def popen(self, host: str, argv: list[str]) -> subprocess.Popen:
        return subprocess.Popen(
            argv,
            stdout=(self.log_dir / f"{host}.stdout").open("w"),
            stderr=subprocess.STDOUT,
            env={**os.environ, "TRANSPORTLAB_HOST": host},
        )


def make_runner(kind: str, scenario: Scenario, log_dir: Path) -> Runner:
    if kind == "loopback":
        return LoopbackRunner(scenario, log_dir)
    raise SystemExit(f"unknown runner {kind!r}. ns3 runs in orchestrator.py")
