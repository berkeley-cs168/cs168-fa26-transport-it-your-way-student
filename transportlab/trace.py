"""Workload cards and the traces built from them.

A card is declarative yaml. `build_trace` turns a card plus a seed into an
ordered list of messages the harness will hand to the sending protocol. Same
card and seed always produce the same trace, so two teams graded on the same
card faced the same work.

Card times are in wire seconds by default, meaning simulated seconds on the
scenario's network as configured. A card may set `time_unit: claimed` to write times in the units the
scenario claims to model, and the loader divides them by the scenario's
`claims.time_factor`. The sensor cards use that so a TA can write "a burst every
30 minutes" instead of "every 30 seconds".
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import random
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import yaml

PATTERNS = ("bulk", "poisson", "incast", "burst")


@dataclass(frozen=True)
class Message:
    """One application message the protocol has to get across."""

    msg_id: int
    t_s: float          # wire seconds after trial start, when the app offers it
    src: str            # host name
    dst: str            # host name
    size: int           # payload bytes
    deadline_s: float | None  # allowance after t_s, wire seconds; None means untimed
    direction: str      # label used to group throughput, e.g. downlink
    # Messages that make up one unit of work, done only when all of them are:
    # one round of an incast flow. Step time is measured per group.
    group: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["group"] is None:
            del d["group"]   # keeps traces without groups byte-identical
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        return cls(**d)


class WorkloadError(ValueError):
    pass


def load_card(path: str | Path) -> dict:
    path = Path(path)
    card = yaml.safe_load(path.read_text())
    if not isinstance(card, dict):
        raise WorkloadError(f"{path} is not a yaml mapping")
    card.setdefault("id", path.stem)
    card.setdefault("time_unit", "wire")
    if card["time_unit"] not in ("wire", "claimed"):
        raise WorkloadError(f"{path}: time_unit must be wire or claimed")
    if "flows" not in card or not card["flows"]:
        raise WorkloadError(
            f"{path} has no flows yet. add at least one under `flows:`; the "
            "comments in the file show how, and "
            "scenarios/<id>/workloads/basic_workload.yaml is a working one to "
            "start from")
    card["_path"] = str(path)
    return card


def expand(pattern: str | list[str], names: Iterable[str]) -> list[str]:
    """Match a host pattern (or list of them) against the topology host names."""
    names = list(names)
    patterns = [pattern] if isinstance(pattern, str) else list(pattern)
    out: list[str] = []
    for p in patterns:
        hit = [n for n in names if fnmatch.fnmatchcase(n, p)]
        if not hit:
            raise WorkloadError(f"host pattern {p!r} matched nothing in {names}")
        out += [n for n in hit if n not in out]
    return out


def build_trace(card: dict, hosts: Iterable[str], time_factor: float = 1.0,
                wire_divisor: float = 1.0) -> list[Message]:
    """Expand a card into a sorted, id-numbered list of messages."""
    hosts = list(hosts)
    seed = int(card.get("seed", 0))
    divisor = float(time_factor) if card.get("time_unit") == "claimed" else float(wire_divisor)
    if divisor <= 0:
        raise WorkloadError("time_factor must be positive")

    raw: list[tuple[float, str, str, int, float | None, str, str | None]] = []
    for index, flow in enumerate(card["flows"]):
        kind = flow.get("pattern", "bulk")
        if kind not in PATTERNS:
            raise WorkloadError(f"unknown pattern {kind!r}, expected one of {PATTERNS}")
        rng = random.Random(f"{seed}:{index}:{kind}")
        raw += _expand_flow(kind, flow, hosts, rng, index)

    raw.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    trace = [
        Message(
            msg_id=i,
            t_s=round(t / divisor, 6),
            src=src,
            dst=dst,
            size=size,
            deadline_s=None if deadline is None else round(deadline / divisor, 6),
            direction=direction,
            group=group,
        )
        for i, (t, src, dst, size, deadline, direction, group) in enumerate(raw)
    ]
    if not trace:
        raise WorkloadError(f"card {card.get('id')} produced no messages")
    return trace


def _expand_flow(kind, flow, hosts, rng, index=0):
    srcs = expand(flow.get("src", flow.get("srcs", "sender*")), hosts)
    dsts = expand(flow.get("dst", flow.get("dsts", "receiver*")), hosts)
    size = int(flow.get("msg_bytes", 1024))
    if size <= 0:
        raise WorkloadError("msg_bytes must be positive")
    deadline = flow.get("deadline_s")
    deadline = None if deadline is None else float(deadline)
    direction = flow.get("direction", "downlink")
    start = float(flow.get("start_s", 0.0))
    out = []

    # A server can be a sender and a receiver with the same number, senderN
    # and receiverN, since one host can't be both. `exclude_self` leaves out
    # a server sending to itself, for all-to-all flows.
    exclude_self = bool(flow.get("exclude_self", False))

    def server(name):
        m = re.fullmatch(r"[a-z]+?(\d+)", name)
        return m.group(1) if m else name

    def emit(t, src, dst, group=None):
        if exclude_self and server(src) == server(dst):
            return
        out.append((max(0.0, t), src, dst, size, deadline, direction, group))

    if kind == "bulk":
        total = flow.get("total_bytes")
        rate = flow.get("rate_bps")
        for src in srcs:
            for dst in dsts:
                if total is not None:
                    count = max(1, int(round(float(total) / size)))
                else:
                    duration = float(flow.get("duration_s", 1.0))
                    if not rate:
                        raise WorkloadError("bulk flow needs total_bytes or rate_bps")
                    count = max(1, int(duration * float(rate) / (size * 8)))
                gap = (size * 8 / float(rate)) if rate else 0.0
                for k in range(count):
                    emit(start + k * gap, src, dst)

    elif kind == "poisson":
        mean = float(flow.get("mean_interval_s", 1.0))
        if mean <= 0:
            raise WorkloadError("mean_interval_s must be positive")
        end = start + float(flow.get("duration_s", 10.0))
        limit = flow.get("count")
        for src in srcs:
            for dst in dsts:
                t, n = start, 0
                while t < end and (limit is None or n < int(limit)):
                    emit(t, src, dst)
                    t += rng.expovariate(1.0 / mean)
                    n += 1

    elif kind == "incast":
        period = float(flow.get("period_s", 1.0))
        if period <= 0:
            raise WorkloadError("period_s must be positive")
        burst = float(flow.get("burst_bytes", size))
        per_src = max(1, int(round(burst / size)))
        rounds = flow.get("rounds")
        rounds = int(rounds) if rounds else max(1, int(float(flow.get("duration_s", 1.0)) / period))
        jitter = float(flow.get("jitter_s", 0.0))
        for r in range(rounds):
            base = start + r * period
            for src in srcs:
                for dst in dsts:
                    offset = rng.uniform(0, jitter) if jitter else 0.0
                    for _ in range(per_src):
                        emit(base + offset, src, dst, group=f"{index}:{r}")

    elif kind == "burst":
        gap_min = float(flow.get("gap_min_s", 1.0))
        gap_max = float(flow.get("gap_max_s", max(gap_min, 2.0)))
        if gap_min <= 0 or gap_max < gap_min:
            raise WorkloadError("need 0 < gap_min_s <= gap_max_s")
        per_burst = int(flow.get("msgs_per_burst", 1))
        end = start + float(flow.get("duration_s", 10.0))
        for src in srcs:
            for dst in dsts:
                t = start
                while t < end:
                    for k in range(per_burst):
                        emit(t + k * float(flow.get("intra_burst_gap_s", 0.0)), src, dst)
                    t += rng.uniform(gap_min, gap_max)

    return out


def trace_hash(trace: list[Message]) -> str:
    """Stable fingerprint of a trace, used to prove two runs saw the same work."""
    blob = json.dumps([m.to_dict() for m in trace], sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


def write_trace(trace: list[Message], path: str | Path) -> None:
    # Compact separators. Every endpoint process reads this file.
    Path(path).write_text(
        json.dumps([m.to_dict() for m in trace], separators=(",", ":"))
    )


def read_trace(path: str | Path) -> list[Message]:
    return [Message.from_dict(d) for d in json.loads(Path(path).read_text())]


def trace_duration(trace: list[Message]) -> float:
    """Wire seconds from trial start to the last offered message."""
    return max(m.t_s for m in trace) if trace else 0.0
