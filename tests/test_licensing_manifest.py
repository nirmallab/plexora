"""Plexora's entitlement table and the product manifest built from it.

The matching rule itself (colon paths, ancestor match) is the `biocognia`
package's and is tested there; this is what Plexora declares with it.
"""

import json

from biocognia import Product, validate_manifest
from biocognia import entitlements as ent

from plexora.licensing import manifest


def test_manifest_is_well_formed():
    for name in manifest.ENTITLEMENTS:
        assert ent.valid(name), name
        assert name != ent.EXPLICIT_FREE
        for parent in ent.ancestors(name):
            assert parent in manifest.ENTITLEMENTS, f"{name}: parent {parent} undeclared"
        label, summary = manifest.ENTITLEMENTS[name]
        assert label and summary.endswith(".")


def test_pro_covers_every_declared_entitlement():
    # The Pro default plus what is off by default is what must cover the
    # manifest: nothing declared may sit outside a root a licence can carry.
    grants = manifest.pro_default() + manifest.OFF_BY_DEFAULT
    for name in manifest.ENTITLEMENTS:
        assert ent.any_satisfies(grants, name), name


def test_the_four_roots_and_the_pro_default():
    assert manifest.ROOTS == ("ai", "volume", "transcriptomics", "mcp")
    # Every root but those off by default, then every first-party plugin:
    # what a purchased licence, or a trial, carries.
    assert manifest.OFF_BY_DEFAULT == ("mcp",)
    assert manifest.pro_default() == ("ai", "volume", "transcriptomics", "plugin:video_studio")
    assert manifest.label("volume") == "Plexora 3D"


def test_surface_studio_is_part_of_plexora_3d_not_ai():
    assert "volume:surfaces" in manifest.ENTITLEMENTS
    assert ent.satisfies("volume", "volume:surfaces")
    assert not ent.any_satisfies(tuple(manifest.AI_MODULES), "volume:surfaces")
    from plexora.agent.core import surface as agent_surface
    from plexora.server import surface

    assert surface.ENTITLEMENT == agent_surface.ENTITLEMENT == "volume:surfaces"


def test_what_is_off_by_default_is_declared_roots_outside_the_ai_root():
    for root in manifest.OFF_BY_DEFAULT:
        assert root in manifest.ROOTS
        assert not ent.any_satisfies(tuple(manifest.AI_MODULES), root), root


def test_unlocks_lists_every_root_and_plugin_with_whether_it_is_granted():
    rows = manifest.unlocks(["ai", "plugin:video_studio"], paid=True)
    assert [row["entitlement"] for row in rows] == [*manifest.ROOTS, "plugin:video_studio"]
    assert {row["entitlement"]: row["granted"] for row in rows} == {
        "ai": True, "volume": False, "transcriptomics": False, "mcp": False, "plugin:video_studio": True}
    assert rows[-1]["label"] == "Video Studio"
    everything = [*manifest.pro_default(), "mcp", "plugin:video_studio"]
    assert all(row["granted"] for row in manifest.unlocks(everything, paid=True))
    assert not any(row["granted"] for row in manifest.unlocks(everything, paid=False))
    # A first-party plugin is part of Pro; only what is off by default is not.
    pro = {r["entitlement"]: r["granted"] for r in manifest.unlocks(manifest.pro_default(), paid=True)}
    assert pro == {"ai": True, "volume": True, "transcriptomics": True, "mcp": False,
                   "plugin:video_studio": True}


def test_every_first_party_plugin_licensed_as_a_whole_is_declared():
    """A shipped plugin that needs a `plugin:<name>` grant is in PLUGINS, so it
    is part of Pro and the platform shows staff a switch for it; nothing in
    PLUGINS is a typo."""
    from plexora.server import plugins as plugin_registry

    named = set()
    for name in plugin_registry.available_names():
        plugin = plugin_registry.load(name)
        if plugin is None:
            continue
        for needed in (plugin.entitlement, *plugin.endpoint_entitlements.values()):
            if needed and needed.startswith(manifest.PLUGIN_ROOT + ":"):
                named.add(needed.split(":", 1)[1])
    assert "video_studio" in named
    assert named == set(manifest.PLUGINS)
    for name, (label, sentence) in manifest.PLUGINS.items():
        assert ent.valid(f"{manifest.PLUGIN_ROOT}:{name}") and label and sentence.endswith("."), name


def test_known_accepts_plugin_paths_but_not_typos():
    assert manifest.known("plugin:spatial_stats")
    assert not manifest.known("ai:gatting")
    assert not manifest.known("plugin")


def test_pro_words_name_the_pro_default_and_what_is_off_by_default():
    words = manifest.pro_words()
    assert words == {"included": "Plexora AI, Plexora 3D, Spatial transcriptomics and Video Studio",
                     "off_by_default": "External MCP access is off by default; "
                                       "an administrator can turn it on."}


def test_a_summary_never_calls_what_is_off_by_default_pro():
    assert manifest.summary("plugin:spatial_stats") == "A plugin from its maker, not part of Plexora Pro."
    assert manifest.summary("plugin:video_studio").startswith("Scientific videos")
    assert manifest.summary("mcp").startswith("Using Plexora's Pro features")


def test_labels_fall_back_to_nearest_declared_ancestor():
    assert manifest.label("ai:gating:session") == "AI gating sessions"
    assert manifest.label("ai:gating:session:future") == "AI gating sessions"
    assert manifest.label("plugin:spatial_stats") == "Spatial Stats"
    assert manifest.label("plugin:video_studio") == "Video Studio"
    assert manifest.summary("plugin:video_studio").startswith("Scientific videos")
    assert manifest.summary("ai:unknown_child").startswith("AI agents")


# -- the product manifest the platform reads ------------------------------------------


def test_the_product_manifest_is_one_the_platform_accepts():
    data = manifest.product_manifest()
    assert validate_manifest(data) == []
    assert data["id"] == "plexora" and data["env_prefix"] == "PLEXORA"
    assert data["free"]["label"] == "Free" and data["limits"] == {}
    # The roots are the declared ones, plus `plugin` for `plugin:<name>`.
    assert sorted(data["entitlements"]) == sorted((*manifest.ROOTS, manifest.PLUGIN_ROOT))
    assert data["entitlements"][manifest.PLUGIN_ROOT]["label"] == "Plugins"
    for root in manifest.ROOTS:
        assert data["entitlements"][root] == {"label": manifest.ENTITLEMENTS[root][0],
                                              "sentence": manifest.ENTITLEMENTS[root][1]}
    assert data["trial_days"] == 30
    # Each first-party plugin, so the platform shows a switch for it.
    assert data["plugins"] == {name: {"label": label, "sentence": sentence}
                               for name, (label, sentence) in manifest.PLUGINS.items()}
    # Deterministic, and plain JSON: the sync tool uploads exactly this.
    assert json.loads(json.dumps(data)) == manifest.product_manifest()


def test_no_prices_or_plans_are_invented_here():
    data = manifest.product_manifest()
    assert not {"plans", "prices", "allowance_micro_per_seat"} & set(data)
    assert not hasattr(manifest, "PLANS")


def test_the_ai_root_opens_exactly_the_task_registrys_modules():
    from plexora.ai import tasks

    modules = {task.module for task in tasks.tasks().values()}
    assert set(manifest.AI_MODULES["ai"]) == modules
    assert manifest.product_manifest()["ai"]["modules"] == {"ai": list(manifest.AI_MODULES["ai"])}


def test_the_product_is_built_from_this_manifest():
    from plexora.licensing import GUARD, LICENSING, PRODUCT

    assert PRODUCT == Product.from_manifest(manifest.product_manifest(), version=PRODUCT.version)
    assert PRODUCT.id == "plexora" and PRODUCT.cli == "plexora"
    assert PRODUCT.env("LICENSE_FILE") == "PLEXORA_LICENSE_FILE"
    assert LICENSING.product is PRODUCT and GUARD.licensing is LICENSING
