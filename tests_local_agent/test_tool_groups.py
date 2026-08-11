"""Regression tests for per-chat tool visibility and the provider safety cap."""
from local_agent.actions.groups import TOOL_GROUPS, group_by_id
from local_agent.actions.registry import Action, Risk
from local_agent.bridge.api.handlers import BridgeHandlers, _TOOL_CAP
from local_agent.core.config import AssistantSettings


def _handlers(tmp_path):
    return BridgeHandlers.build(AssistantSettings(data_dir=tmp_path, work_dir=tmp_path))


def test_every_action_has_valid_group(tmp_path):
    h = _handlers(tmp_path)
    valid = {g.id for g in TOOL_GROUPS}
    assert h.registry.all()
    assert all(a.group in valid for a in h.registry.all())


def test_visibility_is_per_session_and_registry_is_preserved(tmp_path):
    h = _handlers(tmp_path)
    all_names = {a.name for a in h.registry.all()}
    h.set_tool_groups("one", ["github"])
    visible = h._visible_tools("one")
    assert all(a.group == "github" for a in visible)
    h.set_tool_groups("one", ["files"])
    assert {a.name for a in h._visible_tools("one")} != all_names
    h.set_tool_groups("one", ["github"])
    assert all(a.group == "github" for a in h._visible_tools("one"))
    assert {a.name for a in h.registry.all()} == all_names


def test_visibility_never_exceeds_cap(tmp_path):
    h = _handlers(tmp_path)
    h.set_tool_groups("one", [g.id for g in TOOL_GROUPS])
    assert len(h._visible_tools("one")) <= _TOOL_CAP
    assert h.tool_groups("one")["enabled_tool_count"] <= _TOOL_CAP
