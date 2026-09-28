"""The entitlement grammar and the one matching rule: a grant covers itself
and everything beneath it, and nothing beside or above it."""

import pytest

from plexora.licensing import entitlements as ent
from plexora.licensing import manifest


@pytest.mark.parametrize("text", ["ai", "ai:gating", "ai:gating:session", "plugin:foo_bar",
                                  "a1:b2"])
def test_valid_entitlements(text):
    assert ent.valid(text)


@pytest.mark.parametrize("text", ["", "AI", "ai:", ":ai", "ai::gating", "ai gating",
                                  "1ai", "ai:Gating", "ai-gating", None, 3, ["ai"],
                                  "a" * 200])
def test_invalid_entitlements(text):
    assert not ent.valid(text)


@pytest.mark.parametrize("grant,required,expected", [
    ("ai", "ai", True),
    ("ai", "ai:gating", True),
    ("ai", "ai:gating:session", True),
    ("ai:gating", "ai:gating:session", True),
    ("ai:gating", "ai", False),                   # a child never covers its parent
    ("ai:gating", "ai:evidence", False),          # nor a sibling
    ("ai:gating:session", "ai:gating:analytics", False),
    ("ai", "aix", False),                         # a prefix is not an ancestor
    ("ai", "aix:gating", False),
    ("ai:gat", "ai:gating", False),
    ("plugin:foo", "plugin:foo:export", True),
    ("plugin:foo", "plugin:foobar", False),
])
def test_hierarchy_truth_table(grant, required, expected):
    assert ent.satisfies(grant, required) is expected


def test_free_requirements_always_pass_even_with_no_grants():
    assert ent.any_satisfies((), None)
    assert ent.any_satisfies((), "free")
    assert not ent.any_satisfies((), "ai")


def test_normalize_drops_junk_and_free_and_sorts():
    assert ent.normalize(["ai:gating", "ai", "ai", "BAD", 7, "free", "ai::x"]) == \
        ("ai", "ai:gating")
    assert ent.normalize(None) == ()
    assert ent.normalize("ai") == ()   # a bare string is not a list of grants


def test_ancestors():
    assert ent.ancestors("ai:gating:session") == ("ai:gating", "ai")
    assert ent.ancestors("ai") == ()


def test_manifest_is_well_formed():
    for name in manifest.ENTITLEMENTS:
        assert ent.valid(name), name
        assert name != ent.EXPLICIT_FREE
        for parent in ent.ancestors(name):
            assert parent in manifest.ENTITLEMENTS, f"{name}: parent {parent} undeclared"
        label, summary = manifest.ENTITLEMENTS[name]
        assert label and summary.endswith(".")


def test_exactly_two_plans_and_free_grants_nothing():
    assert manifest.PLANS == ("free", "paid")
    assert set(manifest.PLAN_ENTITLEMENTS) == {"free", "paid"}
    assert manifest.PLAN_ENTITLEMENTS["free"] == ()
    for grant in manifest.PLAN_ENTITLEMENTS["paid"]:
        assert manifest.known(grant)


def test_paid_covers_every_declared_entitlement():
    grants = manifest.PLAN_ENTITLEMENTS["paid"]
    for name in manifest.ENTITLEMENTS:
        assert ent.any_satisfies(grants, name), name


def test_known_accepts_plugin_paths_but_not_typos():
    assert manifest.known("plugin:spatial_stats")
    assert not manifest.known("ai:gatting")
    assert not manifest.known("plugin")


def test_labels_fall_back_to_nearest_declared_ancestor():
    assert manifest.label("ai:gating:session") == "AI gating sessions"
    assert manifest.label("ai:gating:session:future") == "AI gating sessions"
    assert manifest.label("plugin:spatial_stats") == "Spatial Stats"
    assert manifest.summary("ai:unknown_child").startswith("AI agents")
