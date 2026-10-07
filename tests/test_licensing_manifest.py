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


def test_paid_covers_every_declared_entitlement():
    # Add-ons are sold separately, so the `ai` root plus every add-on is what
    # must cover the manifest.
    grants = tuple(manifest.AI_MODULES) + manifest.ADD_ONS
    for name in manifest.ENTITLEMENTS:
        assert ent.any_satisfies(grants, name), name


def test_add_ons_are_declared_roots_outside_the_ai_root():
    for root in manifest.ADD_ONS:
        assert root in manifest.ROOTS
        assert not ent.any_satisfies(tuple(manifest.AI_MODULES), root), root


def test_unlocks_lists_every_root_with_whether_it_is_granted():
    rows = manifest.unlocks(["ai"], paid=True)
    assert [row["entitlement"] for row in rows] == list(manifest.ROOTS)
    assert {row["entitlement"]: row["granted"] for row in rows} == {"ai": True, "mcp": False}
    assert all(row["granted"] for row in manifest.unlocks(["ai", "mcp"], paid=True))
    assert not any(row["granted"] for row in manifest.unlocks(["ai", "mcp"], paid=False))


def test_known_accepts_plugin_paths_but_not_typos():
    assert manifest.known("plugin:spatial_stats")
    assert not manifest.known("ai:gatting")
    assert not manifest.known("plugin")


def test_labels_fall_back_to_nearest_declared_ancestor():
    assert manifest.label("ai:gating:session") == "AI gating sessions"
    assert manifest.label("ai:gating:session:future") == "AI gating sessions"
    assert manifest.label("plugin:spatial_stats") == "Spatial Stats"
    assert manifest.summary("ai:unknown_child").startswith("AI agents")


# -- the product manifest the platform reads ------------------------------------------


def test_the_product_manifest_is_one_the_platform_accepts():
    data = manifest.product_manifest()
    assert validate_manifest(data) == []
    assert data["id"] == "plexora" and data["env_prefix"] == "PLEXORA"
    assert data["free"]["label"] == "Free" and data["limits"] == {}
    # The roots are the declared ones, plus `plugin` for `plugin:<name>`.
    assert sorted(data["entitlements"]) == sorted((*manifest.ROOTS, manifest.PLUGIN_ROOT))
    for root in manifest.ROOTS:
        assert data["entitlements"][root] == {"label": manifest.ENTITLEMENTS[root][0],
                                              "sentence": manifest.ENTITLEMENTS[root][1]}
    assert data["trial_days"] == 30
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
