"""dagentos.providerkit.tools — the registry both inner harnesses offer from, tested once
here rather than once per harness: selection by declared effect class, the definition
error for unknown names, duplicate registration, entry-point loading (a plugin that raises
is skipped with a warning naming it, an unknown group is simply empty), and the
openai-agents provider's legacy group alias."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from dagentos.core.models import EffectClass
from dagentos.providerkit import tools
from dagentos.providerkit.prompt import strip_fences


def send_mail(to: str, body: str) -> str:
    """Send an email."""
    return "sent"


def test_select_splits_named_tools_by_declared_class_and_keeps_order():
    reg = tools.ToolRegistry([*tools.BUILTIN_TOOLS,
                              tools.ToolSpec("send_mail", send_mail, EffectClass.send_message)])
    offered, withheld = reg.select(["send_mail", "word_count", "utc_now"],
                                   frozenset({EffectClass.compute}))
    assert [s.name for s in offered] == ["word_count", "utc_now"]
    assert [s.name for s in withheld] == ["send_mail"]
    offered, withheld = reg.select(["send_mail"], frozenset({EffectClass.compute,
                                                             EffectClass.send_message}))
    assert [s.name for s in offered] == ["send_mail"] and withheld == []
    assert reg.describe() == {"send_mail": "send_message", "utc_now": "compute",
                              "word_count": "compute"}


def test_unknown_name_and_duplicate_registration_are_definition_errors():
    reg = tools.ToolRegistry(tools.BUILTIN_TOOLS)
    with pytest.raises(tools.UnknownTool, match=r"'nope' is not registered.*utc_now, word_count"
                                                r".*agentos\.tools"):
        reg.select(["nope"], frozenset())
    with pytest.raises(ValueError, match="registered twice"):
        reg.add(tools.BUILTIN_TOOLS[0])


def test_load_registry_skips_a_broken_plugin_naming_it(monkeypatch, caplog):
    good = tools.ToolSpec("send_mail", send_mail, EffectClass.send_message)

    def fake_entry_points(*, group):
        if group == "agentos.tools":
            return [SimpleNamespace(name="mail", load=lambda: (lambda: [good])),
                    SimpleNamespace(name="broken", load=lambda: (lambda: 1 / 0))]
        return []                                              # any other group: nothing

    monkeypatch.setattr(tools, "entry_points", fake_entry_points)
    with caplog.at_level(logging.WARNING, logger="agentos.providerkit.tools"):
        reg = tools.load_registry(entry_point_groups=("agentos.tools", "agentos.legacy_group"))
    assert reg.names() == ["send_mail", "utc_now", "word_count"]
    assert "plugin 'broken' (agentos.tools) skipped" in caplog.text
    assert tools.load_registry(builtins=False, entry_point_groups=()).names() == []


def test_openai_agents_provider_still_loads_its_legacy_group(monkeypatch):
    """One release of compatibility: a plugin registered under the pre-kit group name
    `agentos.openai_agents_tools` is still offered by the openai-agents harness."""
    pytest.importorskip("agentos_provider_openai_agents")
    from agentos_provider_openai_agents import tools as sdk_tools

    legacy = tools.ToolSpec("legacy_tool", send_mail, EffectClass.send_message)
    seen_groups: list[str] = []

    def fake_entry_points(*, group):
        seen_groups.append(group)
        if group == sdk_tools.LEGACY_ENTRY_POINT_GROUP:
            return [SimpleNamespace(name="old", load=lambda: (lambda: [legacy]))]
        return []

    monkeypatch.setattr(tools, "entry_points", fake_entry_points)
    reg = sdk_tools.load_registry()
    assert "legacy_tool" in reg.names()
    assert seen_groups == ["agentos.tools", "agentos.openai_agents_tools"]


def test_strip_fences_is_shared_and_never_truncates_a_body_with_backticks():
    assert strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_fences("```\n{}\n```  ") == "{}"
    assert strip_fences('{"a": 1}') == '{"a": 1}'
    body = 'say `hi` and ``` then stop'
    assert strip_fences(body) == body
    assert strip_fences("```json\n" + body) == body          # opening fence, no closing one
