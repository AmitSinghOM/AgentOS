"""Webhook authentication: the scheme Stripe, Slack and GitHub-style senders use.

    X-AgentOS-Timestamp: <unix seconds>
    X-AgentOS-Signature: v1=<hex HMAC-SHA256 over "{timestamp}.{raw body}">
    X-AgentOS-Delivery:  <sender's delivery id>          (optional)

The timestamp is inside the signed string, so a captured request cannot be replayed after
the window (default 300 s either side of the receiver's clock) — and a replay *inside* the
window is harmless because the delivery id (or, failing that, the body hash) is the run's
`Idempotency-Key`, so the same delivery maps to the same run. Comparison is constant-time.
The secret is a shared credential the operator mints once and gives the sender; it lives in
an environment variable named by the triggers file, never in the file.
"""
from __future__ import annotations

import hashlib
import hmac
import time

SCHEME = "v1"
DEFAULT_TOLERANCE_SECONDS = 300
MIN_SECRET_LENGTH = 16


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"{SCHEME}={mac.hexdigest()}"


def verify(secret: str, timestamp: str, body: bytes, signature: str, *,
           now: float | None = None, tolerance: int = DEFAULT_TOLERANCE_SECONDS) -> bool:
    """True only when the timestamp parses, sits within `tolerance` of `now`, and the
    signature is this scheme's HMAC over `{timestamp}.{body}`. Never raises."""
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
    expected = sign(secret, ts, body)
    return hmac.compare_digest(expected.encode(), signature.encode())


def delivery_key(name: str, delivery_id: str | None, body: bytes) -> str:
    """The run's `Idempotency-Key` for one delivery: the sender's id when it gives one,
    else the hash of what it sent."""
    if delivery_id:
        return f"webhook:{name}:{delivery_id}"
    return f"webhook:{name}:sha256:{hashlib.sha256(body).hexdigest()}"
