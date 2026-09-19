"""The tool registry every inner harness shares: what an SDK agent may call, and what each
call MEANS to the governor.

Tools are the operator's, never the model's or the agent JSON's. An AgentOS agent's
`config.tools` names entries here by string; there is no tool code in agent definitions.
Every registered tool carries an `EffectClass`, and at dispatch an inner-harness executor
offers the model ONLY the tools whose class the AgentOS agent declared — an undeclared
tool is not offered-and-refused, it is simply not there (the governor's tier-2/tier-3 gate
has already ruled on the declared set before the step ran). Each tool the model actually
calls is reported back as an `Effect`, so `_settle`'s undeclared-effect check remains the
backstop.

This module has no SDK import: the same `ToolSpec` is handed to the OpenAI Agents SDK's
`function_tool` and to PydanticAI's `Tool`, so one registration serves every harness.
Operators register tools through the `agentos.tools` entry-point group: each entry resolves
to a callable returning `Iterable[ToolSpec]`. Two harmless built-ins (`compute` class)
exist so the quickstart and tests need no plugin.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import entry_points
from typing import Any

from agentos.core.models import EffectClass

_log = logging.getLogger("agentos.providerkit.tools")

ENTRY_POINT_GROUP = "agentos.tools"


@dataclass(frozen=True)
class ToolSpec:
    """A Python callable an inner harness may expose to the model, with the effect class
    the governor should charge for calling it. `fn` must have type-annotated parameters
    and a docstring: every SDK derives the JSON schema the model sees from them."""

    name: str
    fn: Callable[..., Any]
    effect_class: EffectClass
    description: str | None = None


class UnknownTool(KeyError):
    """An agent named a tool that is not registered. The message lists what is."""


class ToolRegistry:
    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.add(spec)

    def add(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} registered twice")
        self._specs[spec.name] = spec

    def names(self) -> list[str]:
        return sorted(self._specs)

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            have = ", ".join(self.names()) or "none"
            raise UnknownTool(f"tool {name!r} is not registered; registered tools: "
                              f"[{have}] — add it via the {ENTRY_POINT_GROUP!r} entry point") \
                from None

    def select(self, names: Iterable[str], declared: frozenset[EffectClass]) \
            -> tuple[list[ToolSpec], list[ToolSpec]]:
        """(offered, withheld): the named tools split by whether their effect class is in
        the step's declared set. Unknown names raise — a definition error, not a policy one."""
        offered, withheld = [], []
        for name in names:
            spec = self.get(name)
            (offered if spec.effect_class in declared else withheld).append(spec)
        return offered, withheld

    def describe(self) -> dict[str, str]:
        return {n: s.effect_class.value for n, s in sorted(self._specs.items())}


# ---------------------------------------------------------------------------- built-ins
def utc_now() -> str:
    """The current date and time in UTC, ISO 8601."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def word_count(text: str) -> int:
    """Number of whitespace-separated words in `text`."""
    return len(text.split())


BUILTIN_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("utc_now", utc_now, EffectClass.compute),
    ToolSpec("word_count", word_count, EffectClass.compute),
)


def load_registry(*, builtins: bool = True,
                  extra: Iterable[ToolSpec] = (),
                  entry_point_groups: Iterable[str] = (ENTRY_POINT_GROUP,)) -> ToolRegistry:
    """Built-ins + every entry point in `entry_point_groups` + `extra`. A plugin that fails
    to load is skipped with a warning naming it (same rule as executors). A harness that
    kept an older group name passes it alongside the shared one."""
    registry = ToolRegistry(BUILTIN_TOOLS if builtins else ())
    for group in entry_point_groups:
        for ep in entry_points(group=group):
            try:
                for spec in ep.load()():
                    registry.add(spec)
            except Exception as exc:  # noqa: BLE001 — one broken plugin must not take the worker down
                _log.warning("tool plugin %r (%s) skipped: %s", ep.name, group, exc)
    for spec in extra:
        registry.add(spec)
    return registry


def describe_offered(specs: Mapping[str, ToolSpec] | Iterable[ToolSpec]) -> list[str]:
    items = specs.values() if isinstance(specs, Mapping) else specs
    return sorted(f"{s.name}:{s.effect_class.value}" for s in items)
