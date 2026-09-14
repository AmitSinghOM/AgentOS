"""Executor plugin discovery — composition-root code, never imported by `agentos.core`.

Model-vendor executors live in their own distributions (`agentos-provider-*`) and
advertise themselves through the entry-point group ``agentos.executors``
(docs/DEVELOPMENT_STRUCTURE.md §2.2)::

    [project.entry-points."agentos.executors"]
    openai-compat = "agentos_provider_openai_compat:load"

The target is a zero-argument callable returning an object that satisfies
`agentos.core.ports.Executor` (or the executor class itself). Configuration comes from the
plugin's own environment variables; the core passes nothing.

A plugin that fails to load is *skipped with a logged warning naming it*, not fatal: one
broken provider must not take the API and every other agent type down. A run that needs
the missing executor fails at dispatch with a message that says which distribution to
install (see `Engine._prepare`).
"""
from __future__ import annotations

import logging
from importlib.metadata import entry_points

from agentos.core.ports import Executor

ENTRY_POINT_GROUP = "agentos.executors"
logger = logging.getLogger("agentos.plugins")


def discover_executors(group: str = ENTRY_POINT_GROUP) -> dict[str, Executor]:
    """Load every executor plugin installed in this environment, keyed by name."""
    found: dict[str, Executor] = {}
    for ep in entry_points(group=group):
        try:
            target = ep.load()
            executor = target() if callable(target) else target
            name = getattr(executor, "name", None) or ep.name
            if not hasattr(executor, "execute"):
                raise TypeError(f"{ep.value} did not produce an Executor (no execute())")
            if name in found:
                logger.warning("executor plugin %r registered twice; keeping the first", name)
                continue
            found[name] = executor
            logger.info("executor plugin %r v%s loaded from %s",
                        name, getattr(executor, "version", "?"), ep.value)
        except Exception as exc:  # noqa: BLE001 — one broken plugin must not kill the process
            logger.warning("executor plugin %r (%s) failed to load and was skipped: %s",
                           ep.name, ep.value, exc)
    return found


def describe(executors: dict[str, Executor]) -> list[dict]:
    """What `GET /executors` shows: name, version, and — when the plugin offers it —
    `describe()` (models, aliases, pricing hash) and `health()` (can it reach its server)."""
    out = []
    for name, ex in sorted(executors.items()):
        item: dict = {"name": name, "version": getattr(ex, "version", None)}
        for hook in ("describe", "health"):
            fn = getattr(ex, hook, None)
            if fn is None:
                continue
            try:
                item[hook] = fn()
            except Exception as exc:  # noqa: BLE001 — surfaced, not raised
                item[hook] = {"error": str(exc)}
        out.append(item)
    return out


def store_pricing_snapshots(executors: dict[str, Executor], blobs) -> dict[str, str]:
    """§11 A6: a provider that prices steps exposes `pricing_snapshot() -> bytes`. Put the
    table in the BlobStore at startup so every `pricing_snapshot_hash` on a step is
    resolvable via `GET /blobs/{sha256}` — in 2033 as well as today."""
    stored: dict[str, str] = {}
    for name, ex in executors.items():
        fn = getattr(ex, "pricing_snapshot", None)
        if fn is None:
            continue
        try:
            ref = blobs.put(fn(), media_type="application/json")
            stored[name] = ref.sha256
        except Exception as exc:  # noqa: BLE001
            logger.warning("executor %r pricing snapshot not stored: %s", name, exc)
    return stored
