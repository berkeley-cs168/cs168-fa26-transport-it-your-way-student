"""transportlab: the CS168 Transport project harness.

Students touch exactly one thing here, the Sender/Receiver API in
`transportlab.api`. Everything else runs the trial and measures it.
"""

__version__ = "0.1.0"
SCHEMA_VERSION = "1.0"

# UDP header (8) + IPv4 header (20). Every datagram costs this much on the wire
# on top of its payload, and the harness charges for it in the overhead metric.
WIRE_HEADER_BYTES = 28
