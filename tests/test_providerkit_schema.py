"""agentos.providerkit.schema — the typed-output contract, tested once for every harness:
a valid reply passes and the hash is stable; the first violation is named by path; an
unusable schema (non-object root, remote $ref, invalid draft) is a definition error at first
use; `format` is never enforced (reproducibility); local refs still resolve with no registry."""
from __future__ import annotations

import re

import pytest

from agentos.providerkit.schema import InvalidOutputSchema, OutputSchema, SchemaViolation

CRITIQUE = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "reason": {"type": "string", "minLength": 1},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "reason"],
    "additionalProperties": False,
}


def test_a_valid_reply_passes_and_the_hash_is_stable_under_key_order():
    a = OutputSchema.from_config({"output_schema": CRITIQUE})
    b = OutputSchema.parse({k: CRITIQUE[k] for k in reversed(list(CRITIQUE))})
    assert a is not None and a.sha256 == b.sha256 and len(a.sha256) == 64
    value = {"score": 4, "reason": "terse", "tags": ["haiku"]}
    assert a.validate(value) is value
    assert OutputSchema.from_config({}) is None


@pytest.mark.parametrize("reply, path, needle", [
    ({"score": 7, "reason": "x"}, "$.score", "greater than the maximum of 5"),
    ({"score": 3}, "$", "'reason' is a required property"),
    ({"score": 3, "reason": ""}, "$.reason", "should be non-empty"),
    ({"score": 3, "reason": "ok", "extra": 1}, "$", "Additional properties"),
    ({"score": 3, "reason": "ok", "tags": ["a", 2]}, "$.tags[1]", "not of type 'string'"),
    ("not an object", "$", "not of type 'object'"),
])
def test_the_first_violation_is_named_by_path(reply, path, needle):
    schema = OutputSchema.parse(CRITIQUE)
    with pytest.raises(SchemaViolation,
                       match="violates output_schema at " + re.escape(path) + ": ") as info:
        schema.validate(reply)
    assert needle in str(info.value) and info.value.schema_sha256 == schema.sha256


def test_a_leaf_failure_beats_the_anyof_wrapper_in_the_message():
    schema = OutputSchema.parse({"type": "object", "properties": {
        "v": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}})
    with pytest.raises(SchemaViolation, match=r"\$\.v: 'x' is not valid under any of the given"):
        schema.validate({"v": "x"})


def test_unusable_schemas_are_definition_errors_at_first_use():
    with pytest.raises(InvalidOutputSchema, match="must be a JSON Schema object, got list"):
        OutputSchema.parse(["not", "a", "schema"])
    with pytest.raises(InvalidOutputSchema, match=r'"type": "object" at the root'):
        OutputSchema.parse({"type": "string"})
    with pytest.raises(InvalidOutputSchema, match=r"remote \$ref \('https://example.invalid/s.json'\)"):
        OutputSchema.parse({"type": "object", "properties": {
            "a": {"$ref": "https://example.invalid/s.json"}}})
    with pytest.raises(InvalidOutputSchema, match=r"not a valid Draft 2020-12 schema at \$\.properties\.a\.type"):
        OutputSchema.parse({"type": "object", "properties": {"a": {"type": "integr"}}})


def test_both_reference_keywords_are_scanned_and_a_dangling_local_ref_is_a_definition_error():
    """Review finding: 2020-12 has TWO reference keywords. A remote `$dynamicRef` used to pass
    parse() and surface at validate() as a resolver crash (no fetch, but not the promised
    definition error). Both are refused up front now, and a LOCAL pointer to nowhere is
    reported as InvalidOutputSchema at validation rather than escaping as a crash."""
    with pytest.raises(InvalidOutputSchema, match=r"remote \$ref \('https://example.invalid/d.json#x'\)"):
        OutputSchema.parse({"type": "object", "properties": {
            "a": {"$dynamicRef": "https://example.invalid/d.json#x"}}})
    dangling = OutputSchema.parse({"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}})
    with pytest.raises(InvalidOutputSchema, match="unresolvable reference"):
        dangling.validate({"a": 1})
    assert dangling.validate({}) == {}          # the ref is never reached, so no error


def test_local_refs_resolve_without_any_registry_and_format_is_not_enforced():
    schema = OutputSchema.parse({
        "type": "object",
        "$defs": {"money": {"type": "object", "properties": {"amount": {"type": "number"}},
                            "required": ["amount"]}},
        "properties": {"price": {"$ref": "#/$defs/money"},
                       "email": {"type": "string", "format": "email"}},
        "required": ["price"],
    })
    assert schema.validate({"price": {"amount": 1.5}, "email": "not-an-email"})
    with pytest.raises(SchemaViolation, match=r"\$\.price: 'amount' is a required property"):
        schema.validate({"price": {}})


def test_the_schema_is_never_templated_and_hashes_into_the_prompt():
    raw = {"type": "object", "properties": {"topic": {"type": "string",
                                                       "description": "about {run.topic}"}}}
    schema = OutputSchema.parse(raw)
    assert "{run.topic}" in schema.prompt_hash_input()       # literal braces survive
    assert schema.schema is raw


def test_a_violation_message_is_capped_so_a_large_reply_never_lands_whole_in_step_failed():
    """Security pass: jsonschema quotes the instance in its message; a root-level type
    mismatch on a big reply would otherwise copy the entire reply into the failure text."""
    schema = OutputSchema.parse({"type": "object", "properties": {"s": {"type": "integer"}}})
    with pytest.raises(SchemaViolation) as info:
        schema.validate({"s": "x" * 5000})
    assert len(str(info.value)) < 400 and str(info.value).endswith("…")
    assert "violates output_schema at $.s:" in str(info.value)
