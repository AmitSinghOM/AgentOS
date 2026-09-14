"""`{run.topic}` / `{summarise.text}` prompt templates over the nested step inputs."""
from __future__ import annotations

import string
from typing import Any

from agentos.providerkit.errors import TemplateError


class _Dotted(string.Formatter):
    def get_field(self, field_name: str, args: Any, kwargs: Any) -> tuple[Any, str]:
        obj: Any = kwargs
        for part in field_name.split("."):
            if isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                raise TemplateError(
                    f"prompt template references {{{field_name}}} but the step inputs have "
                    f"no such field; available top-level keys: "
                    f"{sorted(kwargs) or 'none (add depends_on or pass run inputs)'}")
        return obj, field_name


def render_prompt(template: str, inputs: dict) -> str:
    return _Dotted().vformat(template, (), inputs)
