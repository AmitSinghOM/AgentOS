"""Webhook authentication: the scheme Stripe, Slack and GitHub-style senders use, with one
addition — the delivery id is inside the signed string.

    X-AgentOS-Timestamp: <unix seconds>
    X-AgentOS-Delivery:  <sender's delivery id>          (optional)
    X-AgentOS-Signature: v1=<hex HMAC-SHA256 over "{timestamp}.{len(delivery)}.{delivery}.{raw body}">

`{delivery}` is the header's value (UTF-8) or empty when absent, and `{len(delivery)}` is its
byte length, so the signed string is `"{ts}.0..{body}"` with no id and, say,
`"{ts}.3.d-1.{body}"` with `d-1`. The length prefix makes the encoding injective (an id
containing "." cannot be re-split against the body). The timestamp is signed, so a captured
request cannot be replayed after the window (default 300 s either side of the receiver's
clock). The delivery id is signed because it is the run's `Idempotency-Key`: if it were
outside the signature, one captured request could be replayed inside the window under a
fresh id each time and start a run per replay (self-review R1). With it signed, a replay
inside the window can only reproduce the SAME key, which maps to the same run. Comparison is
constant-time. The secret is a shared credential the operator mints once and gives the
sender; it lives in an environment variable named by the triggers file, never in the file.
"""
from __future__ import annotations

import hashlib
import hmac
import time

SCHEME = "v1"
DEFAULT_TOLERANCE_SECONDS = 300
MIN_SECRET_LENGTH = 16


def _message(timestamp: int, delivery: str | None, body: bytes) -> bytes:
    # Injective: the delivery id is length-prefixed, so an id containing "." (or anything)
    # can never be re-split against the body. The self-review's first cut used a bare
    # "{ts}.{delivery}.{body}" and the lock test found the collision
    # ("a.b" + "x" == "a" + "b.x").
    d = (delivery or "").encode()
    return f"{timestamp}.{len(d)}.".encode() + d + b"." + body


def sign(secret: str, timestamp: int, body: bytes, delivery: str | None = None) -> str:
    mac = hmac.new(secret.encode(), _message(timestamp, delivery, body), hashlib.sha256)
    return f"{SCHEME}={mac.hexdigest()}"


def verify(secret: str, timestamp: str, body: bytes, signature: str, *,
           delivery: str | None = None, now: float | None = None,
           tolerance: int = DEFAULT_TOLERANCE_SECONDS) -> bool:
    """True only when the timestamp parses, sits within `tolerance` of `now`, and the
    signature is this scheme's HMAC over `{timestamp}.{delivery}.{body}`. Never raises."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    if abs(now - ts) > tolerance:
        return False
    scheme, _, digest = (signature or "").partition("=")
    if scheme != SCHEME or not digest:
        return False
    expected = sign(secret, ts, body, delivery)
    return hmac.compare_digest(expected.encode(), signature.encode())


def delivery_key(name: str, delivery_id: str | None, body: bytes) -> str:
    """The run's `Idempotency-Key` for one delivery: the sender's id when it gives one,
    else the hash of what it sent."""
    if delivery_id:
        return f"webhook:{name}:{delivery_id}"
    return f"webhook:{name}:sha256:{hashlib.sha256(body).hexdigest()}"
