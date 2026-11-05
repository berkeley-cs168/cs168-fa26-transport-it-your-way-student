"""One endpoint process. The orchestrator launches these, students do not.

Runs on loopback, loads the student protocol, hands the
sender its slice of the trace, and writes endpoint-<name>.json on the way out.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from .api import AppMessage
from .protocols import load
from .trace import read_trace


def _messages(trace, host, role, seed):
    field = "src" if role == "sender" else "dst"
    return [
        AppMessage(
            msg_id=m.msg_id,
            dst=m.dst,
            size=m.size,
            deadline_s=m.deadline_s,
            offered_s=m.t_s,
            _seed=seed,
        )
        for m in trace
        if getattr(m, field) == host
    ]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="transportlab.run_endpoint")
    p.add_argument("--role", required=True, choices=["sender", "receiver"])
    p.add_argument("--name", required=True)
    p.add_argument("--bind-ip", default="0.0.0.0")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--peers", required=True, help="json mapping host to [ip, port]")
    p.add_argument("--protocol", required=True)
    p.add_argument("--trace", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--t0", type=float, required=True)
    p.add_argument("--until", type=float, required=True)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)

    out = Path(args.out)
    peers = {h: (ip, int(port)) for h, (ip, port) in json.loads(args.peers).items()}
    peers.pop(args.name, None)
    trace = read_trace(args.trace)
    messages = _messages(trace, args.name, args.role, args.seed)

    try:
        sender_cls, receiver_cls = load(args.protocol)
    except Exception:
        traceback.print_exc()
        (out / f"endpoint-{args.name}.json").write_text(json.dumps({
            "endpoint": args.name, "role": args.role, "crashed": True,
            "counters": {}, "events": [{"kind": "protocol_load_failed",
                                        "error": traceback.format_exc(limit=6)}],
        }))
        return 2

    common = dict(
        peers=peers, t0=args.t0, seed=args.seed, out_dir=out,
    )
    if args.role == "sender":
        endpoint = sender_cls(args.name, args.bind_ip, args.port,
                              schedule=messages, **common)
    else:
        endpoint = receiver_cls(args.name, args.bind_ip, args.port,
                                expected=messages, **common)

    endpoint.run(args.until)
    return 1 if getattr(endpoint, "crashed", False) else 0


if __name__ == "__main__":
    sys.exit(main())
