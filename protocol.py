"""Your protocol.

    make run SCENARIO=leo              # in ns-3, the simulated network

What the base class gives you, on both Sender and Receiver:

    self.send_datagram(data, to=None)   put bytes on the wire, at most 1472 of
                                        them. `to` is a peer name or an address;
                                        it can be left out when there is only
                                        one peer
    self.set_timer(name, delay_s)       call on_timer(name) after delay_s.
                                        setting it again replaces it
    self.cancel_timer(name)
    self.timer_pending(name)
    self.now()                          seconds since the trial started
    self.peers                          peer name -> address
    self.log(*parts)                    a line in runs/<run>/<host>.log
    self.stop()                         leave the event loop

Receiver only:

    self.deliver(msg_id, payload)       hand a complete message to the
                                        application. only correct bytes count
    self.delivered(msg_id)              True if that message already counted

Set SCENARIO below to the scenario you chose; the Gradescope autograder
refuses a protocol.py without it.

Every byte you send is counted, headers included, so overhead can be measured
example/baseline_stopwait/protocol.py shows a simple example protocol
transportlab/api.py has relevant functions.
"""

from __future__ import annotations

from transportlab.api import Receiver, Sender

# The scenario your team chose: "leo", "datacenter" or "sensor". Gradescope
# grades your protocol on this scenario, and rejects it while this is empty.
SCENARIO = ""


class MySender(Sender):
    def on_start(self):
        """Once, before any messages arrive. Set up your state here."""

    def on_app_data(self, msg):
        """The application has a message for you. You'll need this.

        msg.msg_id, msg.payload (bytes), msg.dst (the receiver's name),
        msg.size, and msg.deadline_s (None when there is no deadline).
        """

    def on_datagram(self, data, addr):
        """A datagram arrived from the network, such as an ack."""

    def on_timer(self, name):
        """A timer you set with self.set_timer(name, ...) went off."""

    def on_stop(self):
        """Once, at the end of the trial."""


class MyReceiver(Receiver):
    def on_start(self):
        """Once, at the start of the trial."""

    def on_datagram(self, data, addr):
        """A datagram arrived from the network. You'll need this.

        Call self.deliver(msg_id, payload) when you have a whole message.
        `addr` is where it came from, if you want to reply.
        """

    def on_timer(self, name):
        """A timer you set with self.set_timer(name, ...) went off."""

    def on_stop(self):
        """Once, at the end of the trial."""


SENDER, RECEIVER = MySender, MyReceiver
