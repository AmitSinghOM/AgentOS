"""The tool registry, shared with every inner harness via `agentos.providerkit.tools`.

Register tools through the `agentos.tools` entry-point group: each entry resolves to a
callable returning `Iterable[ToolSpec]`. The same registration serves the OpenAI Agents SDK
harness; nothing here is PydanticAI-specific — the executor wraps each `ToolSpec.fn` in a
`pydantic_ai.Tool` at dispatch.
"""
from agentos.providerkit.tools import (
    BUILTIN_TOOLS,
    ENTRY_POINT_GROUP,
    ToolRegistry,
    ToolSpec,
    UnknownTool,
    describe_offered,
    load_registry,
    utc_now,
    word_count,
)

__all__ = ["BUILTIN_TOOLS", "ENTRY_POINT_GROUP", "ToolRegistry", "ToolSpec", "UnknownTool",
           "describe_offered", "load_registry", "utc_now", "word_count"]
