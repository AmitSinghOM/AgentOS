"""Typed structured output: an operator-declared JSON Schema that a model reply must satisfy.

Agent definitions are JSON, so the contract is a JSON Schema object under `config.output_schema`
rather than a Python type. The inner harnesses hand the schema to their SDK so the model sees it
(PydanticAI via `PromptedOutput(StructuredDict(...))`, the Agents SDK via an `AgentOutputSchemaBase`
adapter) — but neither SDK ENFORCES it: PydanticAI's `StructuredDict` validates only "is a JSON
object", and the Agents SDK delegates validation to the adapter. So the contract is enforced
here, once, identically for every harness, and the schema's SHA-256 lands on `step.completed`
as `schema_sha256` — the thing a downstream step or an evaluator pins to.

Boundaries:
- The schema is definition, never input: it comes from `agent.config`, immutable per agent
  version, never templated. A schema is data about the shape of the answer; it cannot widen
  anything — validation only narrows what is accepted, and a violation fails the step.
- No egress: `$ref` to a remote document is refused at first use (`Draft202012Validator`
  with an empty registry, and an explicit scan so the refusal names the ref rather than
  surfacing as an unresolvable-reference error mid-validation).
- No `format` checking: `email` / `uri` checkers depend on optional packages and vary by
  environment, which would make the verdict non-reproducible across hosts.
- Draft 2020-12 only. `type: object` at the root, so the value can land in `output["json"]`
  where the untyped `json_output` path already puts it.

Requires the `agentos[providerkit]` extra (`jsonschema`); the core never imports this.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from dagentos.providerkit.errors import BadResponse

CONFIG_KEY = "output_schema"
# jsonschema quotes the offending instance in its messages (`'x' is not valid…`, `{…} is not
# of type 'object'`). A root-level mismatch on a large reply would otherwise put the whole
# reply into step.failed; the log holds the reply on success anyway, so this is a size cap,
# not a secrecy control.
MAX_VIOLATION_MESSAGE = 300


class SchemaViolation(BadResponse):
    """The model's reply did not satisfy the operator's `output_schema`. `path` is the JSON
    Pointer-ish location of the first violation (`$.items[2].price`); the message says what
    was expected. It is a `BadResponse`, so the core records the step as failed and the
    node's retry policy decides, exactly like non-JSON on the `json_output` path."""

    def __init__(self, path: str, message: str, *, schema_sha256: str) -> None:
        if len(message) > MAX_VIOLATION_MESSAGE:
            message = message[:MAX_VIOLATION_MESSAGE] + "…"
        super().__init__(f"reply violates output_schema at {path}: {message}")
        self.path = path
        self.schema_sha256 = schema_sha256


class InvalidOutputSchema(BadResponse):
    """The operator's schema is itself unusable (not an object schema, a remote `$ref`, or
    not a valid Draft 2020-12 document). A definition error, surfaced at first use with the
    reason, so a typo cannot silently accept every reply."""


@dataclass(frozen=True)
class OutputSchema:
    schema: Mapping[str, Any]
    sha256: str
    _validator: Draft202012Validator

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> OutputSchema | None:
        """`None` when the agent declares no `output_schema`."""
        raw = cfg.get(CONFIG_KEY)
        if raw is None:
            return None
        return cls.parse(raw)

    @classmethod
    def parse(cls, raw: Any) -> OutputSchema:
        if not isinstance(raw, Mapping):
            raise InvalidOutputSchema(f"{CONFIG_KEY} must be a JSON Schema object, "
                                      f"got {type(raw).__name__}")
        if raw.get("type") != "object":
            raise InvalidOutputSchema(f"{CONFIG_KEY} must have \"type\": \"object\" at the root "
                                      f"(the validated value lands in output[\"json\"])")
        remote = _find_remote_ref(raw)
        if remote is not None:
            raise InvalidOutputSchema(f"{CONFIG_KEY} contains a remote $ref ({remote!r}); only "
                                      f"local refs (#/...) are allowed — a schema must never "
                                      f"trigger a network fetch")
        try:
            Draft202012Validator.check_schema(raw)
        except SchemaError as exc:
            raise InvalidOutputSchema(f"{CONFIG_KEY} is not a valid Draft 2020-12 schema at "
                                      f"{_pointer(exc.absolute_path)}: {exc.message}") from exc
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        # An empty registry: local `#/...` refs resolve within the document; anything else
        # was refused above, so no resolver can ever reach for the network.
        validator = Draft202012Validator(raw, registry=Registry())
        return cls(schema=raw, sha256=hashlib.sha256(canonical.encode()).hexdigest(),
                   _validator=validator)

    def validate(self, value: Any) -> dict[str, Any]:
        """Return `value` when it satisfies the schema; raise `SchemaViolation` naming the
        first violation otherwise. Errors are ordered by `best_match` relevance so the message
        points at the most specific failure, not at an `anyOf` wrapper."""
        try:
            errors = sorted(self._validator.iter_errors(value), key=_relevance)
        except Unresolvable as exc:
            # A reference the scan above could not classify (a local pointer to nowhere, a
            # missing $anchor). Still a DEFINITION error, still no fetch — never a crash.
            raise InvalidOutputSchema(f"{CONFIG_KEY} has an unresolvable reference: {exc}") from exc
        if errors:
            first = errors[0]
            raise SchemaViolation(_pointer(first.absolute_path), first.message,
                                  schema_sha256=self.sha256)
        if not isinstance(value, dict):        # unreachable with a root object schema; belt
            raise SchemaViolation("$", f"expected an object, got {type(value).__name__}",
                                  schema_sha256=self.sha256)
        return value

    def prompt_hash_input(self) -> str:
        """Canonical text for inclusion in provenance `prompt_hash`: the schema changes what
        the model is told, so two prompts that differ only in schema must hash differently."""
        return json.dumps(self.schema, sort_keys=True, separators=(",", ":"))


def _relevance(err: ValidationError) -> tuple:
    # jsonschema's own best_match heuristic, inverted for `sorted`: deeper paths first, then
    # the "weak" combinators (anyOf/oneOf) last so a leaf failure wins.
    weak = err.validator in ("anyOf", "oneOf")
    return (weak, -len(err.absolute_path), err.validator or "")


def _pointer(path) -> str:
    out = "$"
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else f".{p}"
    return out


def _find_remote_ref(node: Any) -> str | None:
    """First `$ref` / `$dynamicRef` that is not a local (`#...`) pointer, or None. 2020-12 has
    two reference keywords; a scan that knew only `$ref` let a remote `$dynamicRef` through
    parse() and surface as a resolver crash at validate() (review finding, fixed here)."""
    if isinstance(node, Mapping):
        for key in ("$ref", "$dynamicRef"):
            ref = node.get(key)
            if isinstance(ref, str) and not ref.startswith("#"):
                return ref
        for v in node.values():
            found = _find_remote_ref(v)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_remote_ref(v)
            if found is not None:
                return found
    return None
