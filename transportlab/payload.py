"""Deterministic message payloads.

The receiver has to check that delivered bytes are the bytes that were sent, but
shipping every payload to both ends in a file would let a protocol read the
answer instead of transferring it. Instead both ends derive the payload from the
message id and the workload seed, and the harness keeps the derivation out of
reach of student code.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache


# Cached because a retransmitting sender asks for the same payload repeatedly and
# a receiver re-derives it on every delivery check. Bounded so a long trial
# cannot grow without limit.
@lru_cache(maxsize=8192)
def payload_for(msg_id: int, size: int, seed: int) -> bytes:
    """Return the canonical `size`-byte payload for a message.

    Same (msg_id, size, seed) always gives the same bytes, so the sender can
    generate them and the receiver can verify them without any shared file.
    """
    if size < 0:
        raise ValueError(f"payload size must be non-negative, got {size}")
    out = bytearray()
    counter = 0
    while len(out) < size:
        block = f"{seed}:{msg_id}:{counter}".encode()
        out += hashlib.sha256(block).digest()
        counter += 1
    return bytes(out[:size])


def payload_digest(msg_id: int, size: int, seed: int) -> str:
    """Short digest of a payload, for logs and mismatch reports."""
    return hashlib.sha256(payload_for(msg_id, size, seed)).hexdigest()[:12]
