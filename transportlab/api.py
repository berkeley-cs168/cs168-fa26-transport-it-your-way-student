"""The API students implement against.

Subclass `Sender` and `Receiver`, implement the hooks, and export them as
SENDER and RECEIVER at the bottom of your module:

    class MySender(Sender):
        def on_app_data(self, msg):
            self.send_datagram(msg.msg_id.to_bytes(4, "big") + msg.payload)

    class MyReceiver(Receiver):
        def on_datagram(self, data, addr):
            self.deliver(int.from_bytes(data[:4], "big"), data[4:])

    SENDER, RECEIVER = MySender, MyReceiver

The base class owns the socket, the event loop, and every counter. There is no
supported way to send a byte that the harness does not see, which is the point:
the overhead metric is only meaningful if it counts everything.

Under ns-3 (`make run`) there is no socket or event loop here: the simulator
calls the hooks, `now()` is simulated time, and the simulator keeps its own
copy of every counter, which is the one that gets graded. Under `make
run-local` this module talks to real UDP sockets.
"""

from __future__ import annotations

import json
import selectors
import socket
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import WIRE_HEADER_BYTES
from .payload import payload_for

# Largest datagram the harness will put on the wire. Chosen so payload plus
# UDP and IPv4 headers fits a 1500 byte MTU without IP fragmentation, because a
# fragmented datagram would quietly cost two packets in the overhead metric.
MAX_DATAGRAM = 1472

_LOG_LINE_CAP = 5000
_BUCKET_S = 0.25

class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppMessage:
    """A message the application wants delivered."""

    msg_id: int
    dst: str
    size: int
    deadline_s: float | None
    offered_s: float          # when the workload said to offer it, wire seconds
    _seed: int = field(repr=False, default=0)

    @property
    def payload(self) -> bytes:
        """The bytes to get across. Derived, so it costs nothing to hold."""
        return payload_for(self.msg_id, self.size, self._seed)


class _Counters:
    __slots__ = (
        "datagrams_sent", "datagrams_recv", "wire_bytes_sent", "wire_bytes_recv",
        "app_bytes_offered", "app_bytes_delivered", "offered", "delivered",
        "duplicates", "corrupt", "unknown", "late",
    )

    def __init__(self):
        for slot in self.__slots__:
            setattr(self, slot, 0)

    def to_dict(self) -> dict:
        return {slot: getattr(self, slot) for slot in self.__slots__}


class Endpoint:
    """Shared machinery. Students do not subclass this directly."""

    role = "endpoint"

    def __init__(
        self,
        name: str,
        bind_ip: str,
        port: int,
        peers: dict[str, tuple[str, int]],
        t0: float,
        seed: int,
        out_dir: str | Path,
        *,
        _sim=None,
    ):
        self.name = name
        self.seed = seed
        self.peers = dict(peers)
        self.max_datagram = MAX_DATAGRAM
        self._t0 = t0
        self._out_dir = Path(out_dir)
        self._counters = _Counters()
        self._timers: dict[str, float] = {}
        self._stopping = False
        self._events: list[dict] = []
        self._log_lines = 0
        self._log_file = (self._out_dir / f"{name}.log").open("w", buffering=1)
        self._wire_buckets: dict[int, int] = {}

        # Under ns-3 the simulator (transportlab._tlsim) stands in for the
        # socket, the clock and the timers. See transportlab/simrun.py.
        self._sim = _sim
        if _sim is not None:
            self.sock = None
            return

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Big buffers on purpose. A drop inside the kernel socket buffer looks
        # like a network loss to the protocol but is not one, and would make the
        # measurement a story about buffer sizing instead of protocol design.
        for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, opt, 4 << 20)
            except OSError:
                pass
        self.sock.bind((bind_ip, port))
        self.sock.setblocking(False)
        self._sel = selectors.DefaultSelector()
        self._sel.register(self.sock, selectors.EVENT_READ)

    # ---- provided to student code -------------------------------------------

    def now(self) -> float:
        """Wire seconds since the trial started. Same clock on every host."""
        if self._sim is not None:
            return self._sim.now()
        return time.monotonic() - self._t0

    def peer(self, name: str) -> tuple[str, int]:
        try:
            return self.peers[name]
        except KeyError:
            raise ProtocolError(
                f"{self.name} has no peer {name!r}. peers: {sorted(self.peers)}"
            ) from None

    def send_datagram(self, data: bytes, to: str | tuple[str, int] | None = None) -> None:
        """Put one datagram on the wire. Counted, always."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ProtocolError(f"send_datagram needs bytes, got {type(data).__name__}")
        if len(data) > self.max_datagram:
            raise ProtocolError(
                f"datagram of {len(data)} bytes exceeds the {self.max_datagram} byte "
                "limit. split it, or the network would fragment it and charge you twice"
            )
        if to is None:
            if len(self.peers) != 1:
                raise ProtocolError(
                    f"{self.name} has {len(self.peers)} peers, so send_datagram needs a "
                    "destination"
                )
            to = next(iter(self.peers))
        addr = self.peer(to) if isinstance(to, str) else to
        if self._sim is not None:
            if not self._sim.send(bytes(data), str(addr[0]), int(addr[1])):
                self._note("send_failed", error=f"no host at {addr[0]}:{addr[1]}")
                return
            self._count_sent(data)
            return
        try:
            self.sock.sendto(bytes(data), addr)
        except OSError as exc:
            # A full transmit queue is the network pushing back. Record it and
            # drop, the same thing a real host would do.
            self._note("send_failed", error=str(exc))
            return
        self._count_sent(data)

    def _count_sent(self, data) -> None:
        self._counters.datagrams_sent += 1
        wire = len(data) + WIRE_HEADER_BYTES
        self._counters.wire_bytes_sent += wire
        bucket = int(self.now() / _BUCKET_S)
        self._wire_buckets[bucket] = self._wire_buckets.get(bucket, 0) + wire

    def set_timer(self, name: str, delay_s: float) -> None:
        """Fire `on_timer(name)` after delay_s. Setting again replaces it."""
        if delay_s < 0:
            raise ProtocolError("timer delay must not be negative")
        if self._sim is not None:
            self._sim.set_timer(str(name), float(delay_s))
            return
        self._timers[name] = self.now() + delay_s

    def cancel_timer(self, name: str) -> None:
        if self._sim is not None:
            self._sim.cancel_timer(str(name))
            return
        self._timers.pop(name, None)

    def timer_pending(self, name: str) -> bool:
        if self._sim is not None:
            return self._sim.timer_pending(str(name))
        return name in self._timers

    def log(self, *parts) -> None:
        """Debug output, capped so one chatty protocol cannot fill the disk."""
        if self._log_lines >= _LOG_LINE_CAP:
            return
        self._log_lines += 1
        line = " ".join(str(p) for p in parts)
        self._log_file.write(f"{self.now():8.3f} {self.name} {line}\n")
        if self._log_lines == _LOG_LINE_CAP:
            self._log_file.write("... log capped\n")

    def stop(self) -> None:
        """Leave the event loop after the current callback."""
        self._stopping = True

    # ---- hooks --------------------------------------------------------------

    def on_start(self) -> None:
        pass

    def on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        pass

    def on_timer(self, name: str) -> None:
        pass

    def on_stop(self) -> None:
        pass

    # ---- harness ------------------------------------------------------------

    def _note(self, kind: str, **fields) -> None:
        self._events.append({"t_s": round(self.now(), 4), "endpoint": self.name,
                             "kind": kind, **fields})

    def _next_wakeup(self) -> float | None:
        return min(self._timers.values()) if self._timers else None

    def _drain_socket(self) -> None:
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError):
                return
            except ConnectionError:
                return
            self._counters.datagrams_recv += 1
            self._counters.wire_bytes_recv += len(data) + WIRE_HEADER_BYTES
            self._guarded(self.on_datagram, data, addr)

    def _fire_timers(self) -> None:
        now = self.now()
        due = [n for n, deadline in self._timers.items() if deadline <= now]
        for name in sorted(due, key=lambda n: self._timers[n]):
            if self._timers.pop(name, None) is None:
                continue
            self._guarded(self.on_timer, name)

    # ---- called by the simulator, through transportlab/simrun.py ----------------

    def _sim_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        self._counters.datagrams_recv += 1
        self._counters.wire_bytes_recv += len(data) + WIRE_HEADER_BYTES
        self._guarded(self.on_datagram, data, addr)

    def _sim_timer(self, name: str) -> None:
        self._guarded(self.on_timer, name)

    def _guarded(self, fn, *a) -> None:
        """Run student code. A crash ends the trial but still yields results."""
        try:
            fn(*a)
        except Exception as exc:
            self._note("protocol_exception", where=fn.__name__, error=repr(exc),
                       traceback=traceback.format_exc(limit=8))
            self.crashed = True
            self.stop()

    def run(self, until_s: float) -> None:
        self.crashed = getattr(self, "crashed", False)
        self._guarded(self.on_start)
        while not self._stopping:
            now = self.now()
            if now >= until_s:
                break
            wakeups = [until_s]
            t = self._next_wakeup()
            if t is not None:
                wakeups.append(t)
            extra = self._pending_work_at()
            if extra is not None:
                wakeups.append(extra)
            timeout = max(0.0, min(wakeups) - now)
            if self._sel.select(timeout):
                self._drain_socket()
            self._fire_timers()
            self._do_pending_work()
        self._guarded(self.on_stop)
        self._finish()

    def _pending_work_at(self) -> float | None:
        return None

    def _do_pending_work(self) -> None:
        pass

    def _extra_stats(self) -> dict:
        return {}

    def _finish(self) -> None:
        stats = {
            "endpoint": self.name,
            "role": self.role,
            "crashed": bool(getattr(self, "crashed", False)),
            "counters": self._counters.to_dict(),
            "events": self._events[:200],
            "wire_buckets": {str(k): v for k, v in sorted(self._wire_buckets.items())},
            "bucket_s": _BUCKET_S,
            **self._extra_stats(),
        }
        (self._out_dir / f"endpoint-{self.name}.json").write_text(json.dumps(stats))
        self._log_file.close()
        if self._sim is None:
            self.sock.close()
            self._sel.close()


class Sender(Endpoint):
    """Gets application messages, has to get them to the receiver."""

    role = "sender"

    def __init__(self, *a, schedule: list[AppMessage] | None = None, **kw):
        super().__init__(*a, **kw)
        self._schedule = sorted(schedule or [], key=lambda m: (m.offered_s, m.msg_id))
        self._next = 0

    def on_app_data(self, msg: AppMessage) -> None:
        raise NotImplementedError("a Sender has to implement on_app_data")

    def _pending_work_at(self) -> float | None:
        if self._next >= len(self._schedule):
            return None
        return self._schedule[self._next].offered_s

    def _do_pending_work(self) -> None:
        now = self.now()
        while self._next < len(self._schedule) and self._schedule[self._next].offered_s <= now:
            msg = self._schedule[self._next]
            self._next += 1
            self._counters.offered += 1
            self._counters.app_bytes_offered += msg.size
            self._guarded(self.on_app_data, msg)
            if self._stopping:
                return

    def _extra_stats(self) -> dict:
        return {"unoffered": len(self._schedule) - self._next}


class Receiver(Endpoint):
    """Takes datagrams off the wire and hands finished messages to the app."""

    role = "receiver"

    def __init__(self, *a, expected: list[AppMessage] | None = None, **kw):
        super().__init__(*a, **kw)
        # Name-mangled on purpose. Reading the expected payloads instead of
        # receiving them is cheating, and the integrity check in metrics.py
        # catches a receiver that delivers more than the wire carried.
        self.__expected = {m.msg_id: m for m in (expected or [])}
        self.__done: set[int] = set()
        self.__samples: list[tuple[int, float, float]] = []   # msg_id, latency, t
        self.__app_buckets: dict[int, int] = {}

    def deliver(self, msg_id: int, payload: bytes) -> bool:
        """Hand a completed message to the application.

        Returns True when it counted. A wrong payload, an unknown id, or a
        repeat does not count, and the reason lands in the results file.
        """
        now = self.now()
        if self._sim is not None:
            # The simulator checks the payload too, and its count is the one
            # graded. This copy keeps the return value and the notes the same.
            self._sim.deliver(int(msg_id), bytes(payload))
        msg = self.__expected.get(int(msg_id))
        if msg is None:
            self._counters.unknown += 1
            self._note("unknown_msg_id", msg_id=int(msg_id))
            return False
        if msg_id in self.__done:
            self._counters.duplicates += 1
            return False
        if len(payload) != msg.size or bytes(payload) != msg.payload:
            self._counters.corrupt += 1
            self._note("payload_mismatch", msg_id=int(msg_id),
                       got_bytes=len(payload), want_bytes=msg.size)
            return False

        self.__done.add(msg_id)
        latency = now - msg.offered_s
        self._counters.delivered += 1
        self._counters.app_bytes_delivered += msg.size
        if msg.deadline_s is not None and latency > msg.deadline_s:
            self._counters.late += 1
        self.__samples.append((int(msg_id), latency, now))
        bucket = int(now / _BUCKET_S)
        self.__app_buckets[bucket] = self.__app_buckets.get(bucket, 0) + msg.size
        return True

    def delivered(self, msg_id: int) -> bool:
        """True if this message already counted. Handy for dropping duplicates."""
        return int(msg_id) in self.__done

    def _extra_stats(self) -> dict:
        return {
            "samples": [
                {"msg_id": i, "latency_s": round(lat, 6), "t_s": round(t, 6)}
                for i, lat, t in self.__samples
            ],
            "app_buckets": {str(k): v for k, v in sorted(self.__app_buckets.items())},
            "expected": len(self.__expected),
        }
