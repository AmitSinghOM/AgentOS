"""Signed chain tail (Phase 8 #3): a seal the DB cannot forge.

The hash chain (`agentos.core.integrity`) is tamper-*evident*: edit, insert or remove an event
and the run stops folding. It has no secret, so an attacker with database access can rewrite a
run and recompute every hash — and once a run is idle (completed, failed, cancelled,
suspended, paused) nothing is appended after it, so nobody would notice.

A **seal** is an `integrity.sealed` event appended in the same batch as each idle/terminal
event, carrying an HMAC-SHA256 over `agentos-seal-v1:{run_id}:{seq}:{hash}` under a key the
database host does not hold. Rewriting anything before a seal requires forging a new one;
deleting the seals shows up as `sealed_through < last_seq`. The seal is itself chained, so its
hash covers the signature.

Keyring file (`AGENTOS_SIGNING_KEYS`), hex secrets of at least 32 bytes, `key_id` per seal so
rotation keeps old seals verifiable:

    {"active": "k1", "keys": {"k1": "<64+ hex>", "k0": "<64+ hex>"}}

Honest limits. Symmetric: whoever holds the key can forge — keep the file off the DB host
(Ed25519 is the next `Signer` through the same seam). Truncation *after* the last seal is
undetectable by any in-log scheme; `SealReport.unsigned_tail` reports it. Unset →
unsigned, one WARNING at startup.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from agentos.core.events import (
    ChainSealed,
    Event,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunPaused,
    RunSuspended,
)

logger = logging.getLogger("agentos.seal")

ALG = "hmac-sha256"
MIN_KEY_BYTES = 32

#: Events after which the log goes idle — the moments a rewrite would otherwise be invisible.
SEAL_AFTER: tuple[type[Event], ...] = (RunCompleted, RunFailed, RunCancelled, RunSuspended,
                                       RunPaused)


class SealError(RuntimeError):
    """Configuration error. The message names the variable or entry and the fix."""


def seal_message(run_id: str, seq: int, hash_: str) -> bytes:
    return f"agentos-seal-v1:{run_id}:{seq}:{hash_}".encode()


class _KeyringFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: str
    keys: dict[str, str]


class HmacKeyring:
    """Signs with `active`, verifies with any listed key."""

    def __init__(self, active: str, keys: dict[str, bytes]) -> None:
        if active not in keys:
            raise SealError(f"active key {active!r} is not in keys {sorted(keys)}")
        self.active = active
        self._keys = keys

    @property
    def key_ids(self) -> list[str]:
        return sorted(self._keys)

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> HmacKeyring:
        p = Path(path)
        where = f"AGENTOS_SIGNING_KEYS={str(p)!r}"
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SealError(f"{where}: file not found") from None
        except json.JSONDecodeError as exc:
            raise SealError(f"{where}: not valid JSON ({exc.msg})") from None
        try:
            parsed = _KeyringFile.model_validate(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(x) for x in first["loc"]) or "<root>"
            raise SealError(f"{where}: {loc}: {first['msg']}") from None
        keys: dict[str, bytes] = {}
        for key_id, hexval in parsed.keys.items():
            try:
                secret = bytes.fromhex(hexval)
            except ValueError:
                raise SealError(f"{where}: keys.{key_id} is not hex") from None
            if len(secret) < MIN_KEY_BYTES:
                raise SealError(f"{where}: keys.{key_id} is {len(secret)} bytes; need at least "
                                f"{MIN_KEY_BYTES} (openssl rand -hex {MIN_KEY_BYTES})")
            keys[key_id] = secret
        if parsed.active not in keys:
            raise SealError(f"{where}: active {parsed.active!r} is not one of keys "
                            f"{sorted(keys)}")
        return cls(parsed.active, keys)

    def sign(self, message: bytes) -> str:
        return hmac.new(self._keys[self.active], message, hashlib.sha256).hexdigest()

    def verify(self, key_id: str, message: bytes, signature: str) -> bool | None:
        """True/False, or None when `key_id` is not in this keyring (cannot judge)."""
        secret = self._keys.get(key_id)
        if secret is None:
            return None
        return hmac.compare_digest(hmac.new(secret, message, hashlib.sha256).hexdigest(), signature)


def seal_for(run_id: str, sealed: Event, keyring: HmacKeyring) -> ChainSealed:
    """The seal event for an already-chained event (it must carry seq and hash)."""
    assert sealed.hash is not None
    return ChainSealed(run_id=run_id, sealed_seq=sealed.seq, sealed_hash=sealed.hash, alg=ALG,
                       key_id=keyring.active,
                       signature=keyring.sign(seal_message(run_id, sealed.seq, sealed.hash)))


@dataclass
class SealReport:
    seals: int = 0
    valid: int = 0
    sealed_through: int | None = None     # highest seq covered by a VALID seal
    unsigned_tail: int = 0                # events after the last valid seal (excluding seals)
    problems: list[str] = field(default_factory=list)
    unknown_keys: list[str] = field(default_factory=list)
    state: str = "unsigned"               # unsigned | verified | unverifiable | INVALID

    def as_dict(self) -> dict:
        return {"state": self.state, "seals": self.seals, "valid": self.valid,
                "sealed_through": self.sealed_through, "unsigned_tail": self.unsigned_tail,
                "problems": self.problems, "unknown_keys": self.unknown_keys}


def verify_seals(events: Sequence[Event], keyring: HmacKeyring | None) -> SealReport:
    """Check every seal in an ordered log: the signature (with the keyring), and that the
    event at `sealed_seq` still carries `sealed_hash`. Without a keyring, seals can be
    matched to the log but not judged (`unverifiable`)."""
    report = SealReport()
    by_seq = {ev.seq: ev for ev in events}
    for ev in events:
        if isinstance(ev, ChainSealed):
            _judge(ev, by_seq, keyring, report)
    covered = report.sealed_through
    report.unsigned_tail = sum(1 for e in events if not isinstance(e, ChainSealed)
                               and (covered is None or e.seq > covered))
    report.state = _state(report, keyring)
    return report


def _judge(ev: ChainSealed, by_seq: dict[int, Event], keyring: HmacKeyring | None,
           report: SealReport) -> None:
    """One seal: does the log still carry the sealed hash, and does the signature hold."""
    report.seals += 1
    target = by_seq.get(ev.sealed_seq)
    if target is None or target.hash != ev.sealed_hash:
        report.problems.append(f"seal at seq {ev.seq}: event {ev.sealed_seq} does not carry "
                               f"the sealed hash")
        return
    if ev.alg != ALG:
        report.problems.append(f"seal at seq {ev.seq}: unknown alg {ev.alg!r}")
        return
    if keyring is None:
        return
    ok = keyring.verify(ev.key_id, seal_message(ev.run_id, ev.sealed_seq, ev.sealed_hash),
                        ev.signature)
    if ok is None:
        if ev.key_id not in report.unknown_keys:
            report.unknown_keys.append(ev.key_id)
        return
    if not ok:
        report.problems.append(f"seal at seq {ev.seq}: signature does not verify "
                               f"(key {ev.key_id!r})")
        return
    report.valid += 1
    report.sealed_through = max(report.sealed_through or 0, ev.sealed_seq)


def _state(report: SealReport, keyring: HmacKeyring | None) -> str:
    if report.problems:
        return "INVALID"
    if report.seals == 0:
        return "unsigned"
    if keyring is None or (report.valid == 0 and report.unknown_keys):
        return "unverifiable"
    return "verified"


def keyring_from_env() -> HmacKeyring | None:
    """Composition-root helper. None (with a WARNING) when AGENTOS_SIGNING_KEYS is unset."""
    path = os.environ.get("AGENTOS_SIGNING_KEYS")
    if not path:
        logger.warning("AGENTOS_SIGNING_KEYS unset: event chains are hash-linked but UNSIGNED — "
                       "anyone with database access can rewrite an idle run. Set "
                       "AGENTOS_SIGNING_KEYS=<keyring file> to seal every idle/terminal event.")
        return None
    keyring = HmacKeyring.from_file(path)
    logger.info("signing keyring loaded from %s (active %s, keys %s)", path, keyring.active,
                keyring.key_ids)
    return keyring


__all__ = ["ALG", "SEAL_AFTER", "HmacKeyring", "SealError", "SealReport", "keyring_from_env",
           "seal_for", "seal_message", "verify_seals"]
