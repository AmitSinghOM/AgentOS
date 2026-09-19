"""Upcaster registry: pure functions that lift a persisted event record from
schema_version N to N+1. Reading always upcasts to CURRENT_SCHEMA_VERSION.

Register with:

    @upcaster("step.completed", from_version=1)
    def _step_completed_v1_to_v2(record: dict) -> dict:
        record["output_ref"] = {...}
        return record

Every upcaster is exercised by the golden corpus (tests/golden/), which is how a log
written by any released version stays replayable by any later version.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dagentos.core.events import CURRENT_SCHEMA_VERSION

Upcaster = Callable[[dict[str, Any]], dict[str, Any]]

_REGISTRY: dict[tuple[str, int], Upcaster] = {}


def upcaster(event_type: str, *, from_version: int) -> Callable[[Upcaster], Upcaster]:
    def register(fn: Upcaster) -> Upcaster:
        key = (event_type, from_version)
        if key in _REGISTRY:
            raise ValueError(f"duplicate upcaster for {key}")
        _REGISTRY[key] = fn
        return fn
    return register


def upcast(record: dict[str, Any]) -> dict[str, Any]:
    """Apply upcasters in sequence until the record is at the current version."""
    event_type = record["event_type"]
    version = int(record.get("schema_version", 1))
    if version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"{event_type} schema_version {version} is newer than this build "
            f"({CURRENT_SCHEMA_VERSION}); upgrade AgentOS to read this log"
        )
    while version < CURRENT_SCHEMA_VERSION:
        fn = _REGISTRY.get((event_type, version))
        if fn is None:
            raise ValueError(f"no upcaster for {event_type} v{version} → v{version + 1}")
        record = fn(record)
        version += 1
        record["schema_version"] = version
    return record


def registered() -> dict[tuple[str, int], Upcaster]:
    return dict(_REGISTRY)
