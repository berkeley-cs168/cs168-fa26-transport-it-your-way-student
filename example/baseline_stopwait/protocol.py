"""Stop and wait.

One message in flight, with fixed timeout. This example is
is correct but also very slow. Your protocol should beat this!

It provides an example of how to use the API: how to frame a
message, how to ack, how to retransmit on a timer.
"""

from __future__ import annotations

import struct

from transportlab.api import Receiver, Sender

# The autograder needs to know which scenario this protocol is for.
SCENARIO = "leo"   # "leo", "datacenter" or "sensor"

# type, msg_id
HEADER = struct.Struct("!BI")
DATA, ACK = 0, 1

RTO_S = 0.5


class StopWaitSender(Sender):
    def on_start(self):
        self.backlog = []      # messages waiting their turn
        self.inflight = None   # the one message we are working on

    def on_app_data(self, msg):
        self.backlog.append(msg)
        self._maybe_send()

    def _maybe_send(self):
        if self.inflight is not None or not self.backlog:
            return
        self.inflight = self.backlog.pop(0)
        self._transmit()

    def _transmit(self):
        msg = self.inflight
        self.send_datagram(HEADER.pack(DATA, msg.msg_id) + msg.payload, to=msg.dst)
        self.set_timer("rto", RTO_S)

    def on_datagram(self, data, addr):
        if len(data) < HEADER.size:
            return
        kind, msg_id = HEADER.unpack(data[:HEADER.size])
        if kind != ACK or self.inflight is None or msg_id != self.inflight.msg_id:
            return
        self.cancel_timer("rto")
        self.inflight = None
        self._maybe_send()

    def on_timer(self, name):
        # No backoff, no RTT estimate. During a blackout this just keeps firing
        # every half second and getting nowhere.
        if name == "rto" and self.inflight is not None:
            self._transmit()


class StopWaitReceiver(Receiver):
    def on_datagram(self, data, addr):
        if len(data) < HEADER.size:
            return
        kind, msg_id = HEADER.unpack(data[:HEADER.size])
        if kind != DATA:
            return
        # Ack unconditionally, including repeats, or a lost ack wedges the sender.
        self.deliver(msg_id, data[HEADER.size:])
        self.send_datagram(HEADER.pack(ACK, msg_id), to=addr)


SENDER, RECEIVER = StopWaitSender, StopWaitReceiver
