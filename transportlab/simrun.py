"""One ns-3 trial, in its own process. The orchestrator launches this.

    python -m transportlab.simrun <run dir>/spec.json

It builds the network in transportlab._tlsim, loads a separate copy of the
protocol module for every host, so module globals are per host as they would
be on separate machines, then hands control to ns-3. ns-3 calls back through
`dispatch`, one host at a time.

Inside a protocol, time.time() and friends return simulated time, `random` is
seeded per host from the trial seed, and time.sleep() and threads raise,
because neither means anything in a simulation.

At the end of run(), the simulator itself writes sim-stats.json, its own
counts, which are the graded ones. This process then leaves with os._exit so
no student code (an atexit hook, say) runs after that.
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
import traceback
from pathlib import Path

from . import _tlsim
from .api import AppMessage
from .payload import payload_for
from .protocols import load
from .trace import read_trace

# Module-level functions of `random` that get rebound to the current host's
# own generator, so hosts do not share one random stream.
_RANDOM_FUNCS = (
    "random", "uniform", "triangular", "randint", "choice", "randrange", "sample",
    "shuffle", "choices", "normalvariate", "lognormvariate", "expovariate",
    "vonmisesvariate", "gammavariate", "gauss", "betavariate", "paretovariate",
    "weibullvariate", "getrandbits", "randbytes", "binomialvariate", "seed",
    "getstate", "setstate",
)


class SimulationError(RuntimeError):
    pass


def _no_sleep(*_):
    raise SimulationError(
        "time.sleep() does nothing useful in the simulator: the clock only moves "
        "between callbacks. use self.set_timer(name, delay) and on_timer instead"
    )


def _no_threads(*_, **__):
    raise SimulationError(
        "threads are not supported in the simulator. use self.set_timer for "
        "anything that should happen later"
    )


def _patch_time() -> None:
    """Make every clock a protocol might read return simulated time."""
    def seconds():
        return _tlsim.now()

    def nanoseconds():
        return round(_tlsim.now() * 1e9)

    for name in ("time", "monotonic", "perf_counter", "process_time", "thread_time"):
        setattr(time, name, seconds)
        setattr(time, name + "_ns", nanoseconds)
    time.sleep = _no_sleep
    threading.Thread.start = _no_threads


class _Host:
    """One host: its endpoint, its random generator, where its prints go."""

    def __init__(self, endpoint, rng: random.Random, stdout):
        self.endpoint = endpoint
        self.rng = rng
        self.stdout = stdout


def _use_random(rng: random.Random) -> None:
    for name in _RANDOM_FUNCS:
        if hasattr(rng, name):
            setattr(random, name, getattr(rng, name))


def _load_failed(out: Path, roles: dict) -> None:
    """The protocol did not import or construct: record it for every host and leave."""
    error = traceback.format_exc(limit=6)
    print(traceback.format_exc(), file=sys.stderr)
    for host in roles:
        (out / f"endpoint-{host}.json").write_text(json.dumps({
            "endpoint": host, "role": roles[host], "crashed": True,
            "counters": {}, "events": [{"kind": "protocol_load_failed", "error": error}],
        }))
    sys.stderr.flush()
    os._exit(2)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    spec_path = Path(argv[0])
    spec = json.loads(spec_path.read_text())
    out = Path(spec["out_dir"])
    seed = int(spec["seed"])
    trace = read_trace(spec["trace"])

    _tlsim.configure(spec["sim"])

    # Who sends and receives what. Payloads are computed here, before any
    # student code is imported, so the simulator's copy can be trusted.
    roles = spec["roles"]
    for m in trace:
        offered_ns = round(m.t_s * 1e9)
        deadline_ns = -1 if m.deadline_s is None else round(m.deadline_s * 1e9)
        _tlsim.offer(m.src, m.msg_id, m.size, offered_ns)
        _tlsim.expect(m.dst, m.msg_id, m.size, offered_ns, deadline_ns,
                      payload_for(m.msg_id, m.size, seed))

    peers_all = {h: (ip, int(port)) for h, (ip, port) in spec["peers"].items()}
    real_stdout = sys.stdout
    hosts: dict[str, _Host] = {}
    # Same module name the loopback runner uses, plus the host, so the label
    # in results.json reads the same under both runners.
    target = Path(spec["protocol"].partition(":")[0])
    stem = target.stem if target.suffix == ".py" else (
        "protocol" if target.is_dir() else str(target).rsplit(".", 1)[-1])
    label = None
    _patch_time()

    for name in sorted(roles):
        role = roles[name]
        peers = {h: a for h, a in peers_all.items() if h != name}
        mine = [m for m in trace if (m.src if role == "sender" else m.dst) == name]
        messages = [AppMessage(msg_id=m.msg_id, dst=m.dst, size=m.size,
                               deadline_s=m.deadline_s, offered_s=m.t_s, _seed=seed)
                    for m in mine]
        ip, port = peers_all[name]
        common = dict(peers=peers, t0=0.0, seed=seed, out_dir=out, _sim=_tlsim)

        rng = random.Random(f"{seed}:{name}")
        stdout = (out / f"{name}.stdout").open("w")
        # Bind this host's random functions and stdout before its module runs,
        # so `from random import randint` at the top of a protocol gets them.
        _use_random(rng)
        sys.stdout = stdout
        try:
            sender_cls, receiver_cls = load(spec["protocol"],
                                            module_name=f"student_protocol_{stem}__{name}")
            if role == "sender":
                endpoint = sender_cls(name, ip, port, schedule=messages, **common)
            else:
                endpoint = receiver_cls(name, ip, port, expected=messages, **common)
        except BaseException:
            sys.stdout = real_stdout
            _load_failed(out, roles)
        sys.stdout = real_stdout
        endpoint.crashed = False
        hosts[name] = _Host(endpoint, rng, stdout)
        if label is None:
            module = sender_cls.__module__.split(".")[-1].split("__")[0]
            label = f"{module}:{sender_cls.__name__}/{receiver_cls.__name__}"

    def dispatch(name, kind, a, b) -> bool:
        host = hosts[name]
        ep = host.endpoint
        _use_random(host.rng)
        sys.stdout = host.stdout
        try:
            if kind == "datagram":
                ep._sim_datagram(a, b)
            elif kind == "timer":
                ep._sim_timer(a)
            elif kind == "offer":
                ep._do_pending_work()
            elif kind == "start":
                ep._guarded(ep.on_start)
            elif kind == "stop":
                ep._guarded(ep.on_stop)
                ep._finish()
                return False
        finally:
            sys.stdout = real_stdout
        return not ep._stopping

    _tlsim.set_label(label)
    _tlsim.run(dispatch, str(out / "sim-stats.json"))

    sys.stdout = real_stdout
    for host in hosts.values():
        host.stdout.close()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
