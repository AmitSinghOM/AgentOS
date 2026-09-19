"""The operator's `output_schema` as an Agents SDK output type.

The SDK sends `json_schema()` to the model as `response_format` and calls `validate_json` on
the reply; it does no validation of its own. This adapter delegates that call to the kit's
`OutputSchema`, so the contract is enforced by the same code — and produces the same message
— as on the PydanticAI harness. A violation is raised as `ModelBehaviorError`, which the
executor already maps to `BadResponse`, keeping the two harnesses' failure vocabulary aligned.
`is_strict_json_schema` is False: operator-written schemas are rarely OpenAI-strict, and a
local Ollama ignores strictness anyway.
"""
from __future__ import annotations

import json
from typing import Any

from agents import AgentOutputSchemaBase, ModelBehaviorError

from agentos.providerkit.prompt import strip_fences
from agentos.providerkit.schema import OutputSchema, SchemaViolation


class KitOutputSchema(AgentOutputSchemaBase):
    def __init__(self, schema: OutputSchema) -> None:
        self._schema = schema

    def is_plain_text(self) -> bool:
        return False

    def name(self) -> str:
        return str(self._schema.schema.get("title") or "output")

    def json_schema(self) -> dict[str, Any]:
        return dict(self._schema.schema)

    def is_strict_json_schema(self) -> bool:
        return False

    def validate_json(self, json_str: str) -> Any:
        try:
            value = json.loads(strip_fences(json_str))
        except ValueError as exc:
            raise ModelBehaviorError(f"reply is not JSON: {json_str[:120]!r}") from exc
        try:
            return self._schema.validate(value)
        except SchemaViolation as exc:
            raise ModelBehaviorError(str(exc)) from exc
