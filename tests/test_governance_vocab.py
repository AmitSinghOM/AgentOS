"""Governance vocabulary is part of the event-log contract (docs/DEVELOPMENT_STRUCTURE.md
§3, §11 A1-A2): values may be added, never renamed or removed. These tests pin the
current set so a future edit that shrinks or renames it fails loudly."""
from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from dagentos.core.models import BlobRef, EffectClass, Principal, PrincipalKind

# Snapshot of the vocabulary as of v0.2.0 planning. Extend by appending; never edit.
EFFECT_CLASSES_V1 = {
    "read", "compute", "write_external", "spend", "send_message", "execute_code", "spawn_run",
}
PRINCIPAL_KINDS_V1 = {"human", "agent", "system"}

# Effect classes whose approval gate requires a human principal unless a workflow opts out.
HUMAN_GATED = {EffectClass.spend, EffectClass.write_external}


def test_effect_classes_never_shrink_or_rename():
    current = {e.value for e in EffectClass}
    missing = EFFECT_CLASSES_V1 - current
    assert not missing, f"effect classes removed/renamed — breaks the log contract: {missing}"


def test_principal_kinds_never_shrink_or_rename():
    current = {k.value for k in PrincipalKind}
    assert PRINCIPAL_KINDS_V1 <= current


def test_human_gated_classes_are_in_the_vocabulary():
    assert HUMAN_GATED <= set(EffectClass)


def test_principal_requires_kind_and_id():
    p = Principal(kind=PrincipalKind.human, id="amit")
    assert p.attestation is None
    with pytest.raises(ValidationError):
        Principal(kind="robot", id="x")  # unknown kind is rejected, not coerced
    with pytest.raises(ValidationError):
        Principal(kind=PrincipalKind.agent)  # id is mandatory


def test_blobref_is_content_addressed_shape():
    data = b'{"hello": "world"}'
    ref = BlobRef(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    assert ref.media_type == "application/json"
    assert len(ref.sha256) == 64
    # Round-trips through JSON without loss (events must be plain JSON, §3).
    assert BlobRef.model_validate_json(ref.model_dump_json()) == ref
