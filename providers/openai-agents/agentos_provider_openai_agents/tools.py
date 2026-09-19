"""The tool registry, re-exported from `dagentos.providerkit.tools` where it moved once a
second inner harness (PydanticAI) needed the identical seam. Nothing here is SDK-specific:
the same `ToolSpec` is handed to the SDK's `function_tool` by the executor.

Register tools through the shared `agentos.tools` entry-point group. The original group,
`agentos.openai_agents_tools`, is still loaded by this provider for one release so an
existing plugin keeps working; move it to `agentos.tools` and it serves every harness.
"""
from __future__ import annotations

from collections.abc import Iterable

from dagentos.providerkit.tools import (
    BUILTIN_TOOLS,
    ENTRY_POINT_GROUP,
    ToolRegistry,
    ToolSpec,
    UnknownTool,
    describe_offered,
    utc_now,
    word_count,
)
from dagentos.providerkit.tools import load_registry as _load_registry

__all__ = ["BUILTIN_TOOLS", "ENTRY_POINT_GROUP", "LEGACY_ENTRY_POINT_GROUP", "ToolRegistry",
           "ToolSpec", "UnknownTool", "describe_offered", "load_registry", "utc_now",
           "word_count"]

LEGACY_ENTRY_POINT_GROUP = "agentos.openai_agents_tools"


def load_registry(*, builtins: bool = True, extra: Iterable[ToolSpec] = (),
                  entry_point_groups: Iterable[str] = (ENTRY_POINT_GROUP,
                                                       LEGACY_ENTRY_POINT_GROUP)) -> ToolRegistry:
    """Built-ins + `agentos.tools` + the legacy `agentos.openai_agents_tools` group + `extra`."""
    return _load_registry(builtins=builtins, extra=extra, entry_point_groups=entry_point_groups)
