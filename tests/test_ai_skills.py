"""The scientific skills: parse, carry every section, and name real tools."""

import pytest

from plexora.agent import registry
from plexora.ai import skills


def test_manifest_lists_the_four_skills():
    names = [s["name"] for s in skills.list_skills()]
    assert names == ["dataset-triage", "visual-inspection", "marker-qc", "visual-gating"]
    assert all(s["available"] for s in skills.list_skills())


def test_every_skill_is_valid_against_the_live_registry():
    registry.discover()
    tools = {cap.tool_name for cap in registry.all_capabilities()}
    assert skills.validate(tools) == []


def test_a_renamed_tool_is_caught():
    assert any("does not exist" in p for p in skills.validate({"list_projects"}))


def test_read_skill():
    assert skills.read_skill("visual-gating").startswith("# Visual gating")
    with pytest.raises(KeyError):
        skills.read_skill("nope")
