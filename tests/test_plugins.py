"""Plugin discovery, activation and applicability.

The end-to-end behaviour (a core build importing nothing, a plugin build
mounting namespaced routes) is covered by test_plugin_boundary.py, which runs
real subprocesses. This file covers the logic in isolation, including the
failure modes a third-party plugin will eventually exercise.
"""

import pytest
from flask import Blueprint

from plexora.api.plugin import Plugin, Requires
from plexora.server import plugins as registry


# --------------------------------------------------------------------------
# Descriptor
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["Gating", "9gating", "gat-ing", "", "gating pkg"])
def test_plugin_names_are_validated(bad):
    """Names become URL segments and SQL identifiers, so they are restricted
    rather than escaped."""
    with pytest.raises(ValueError):
        Plugin(name=bad, label="X")


def test_routes_are_namespaced_by_name():
    assert Plugin(name="roi", label="ROI").url_prefix == "/plugins/roi"


def test_asset_urls_are_namespaced_and_version_stamped():
    """A plugin declares bare filenames; core builds the URL. That is what lets
    the eager and lazy paths agree without a hand-synced ?v= string."""
    plugin = Plugin(name="roi", label="ROI", version="7", scripts=("a.js",), styles=("a.css",))
    assert plugin.asset_urls("scripts") == ["/plugins/roi/static/a.js?v=7"]
    assert plugin.asset_urls("styles") == ["/plugins/roi/static/a.css?v=7"]


def test_asset_urls_respect_a_mounted_base_url():
    """Under Jupyter's proxy the app is not at the server root. Core's own
    template tags use ../client/... and resolve wrong there; plugin assets must
    not inherit that."""
    plugin = Plugin(name="roi", label="ROI", version="7", scripts=("a.js",))
    assert plugin.asset_urls("scripts", "/user/x/proxy/8000") == [
        "/user/x/proxy/8000/plugins/roi/static/a.js?v=7"
    ]


def test_describe_is_what_the_navbar_gets():
    assert Plugin(name="roi", label="ROI").describe() == {"name": "roi", "label": "ROI"}


# --------------------------------------------------------------------------
# Applicability
# --------------------------------------------------------------------------

FULL = {"image_kind": "ome_tiff", "has_feature_data": True,
        "featureData": [{}], "segmentation": "/seg.tif"}


def test_a_plugin_needing_nothing_applies_to_any_real_image():
    assert Requires().satisfied_by(FULL)
    assert Requires().satisfied_by({"image_kind": "ome_tiff"})


def test_rgb_quick_view_is_excluded_by_default():
    """The flat RGB path has no channels and no feature data, so marker tools
    are meaningless there."""
    assert not Requires().satisfied_by({**FULL, "image_kind": "rgb"})


def test_table_requirement_rejects_a_project_without_one():
    assert Requires(table=True).satisfied_by(FULL)
    assert not Requires(table=True).satisfied_by({"image_kind": "ome_tiff", "featureData": []})
    assert not Requires(table=True).satisfied_by(
        {"image_kind": "ome_tiff", "featureData": [{}], "has_feature_data": False}
    )


def test_table_requirement_honours_legacy_projects():
    """Projects registered before has_feature_data existed recorded a
    featureData entry and nothing else."""
    assert Requires(table=True).satisfied_by({"image_kind": "ome_tiff", "featureData": [{}]})


def test_segmentation_requirement():
    assert Requires(segmentation=True).satisfied_by(FULL)
    assert not Requires(segmentation=True).satisfied_by({**FULL, "segmentation": None})


# --------------------------------------------------------------------------
# PLEXORA_PLUGINS parsing -- unset and "" must stay distinguishable
# --------------------------------------------------------------------------

def test_unset_means_everything_installed():
    assert registry.requested(env={}) is None


def test_empty_string_means_a_deliberate_core_build():
    assert registry.requested(env={"PLEXORA_PLUGINS": ""}) == []


def test_names_are_parsed_and_trimmed():
    assert registry.requested(env={"PLEXORA_PLUGINS": "gating, roi ,"}) == ["gating", "roi"]


# --------------------------------------------------------------------------
# Installation
# --------------------------------------------------------------------------

class FakeApp:
    def __init__(self):
        self.config = {}
        self.registered = []

    def register_blueprint(self, blueprint, url_prefix=None):
        self.registered.append((blueprint.name, url_prefix))


def _fake_plugin(name, **kw):
    return Plugin(
        name=name, label=name.title(),
        blueprint_factory=lambda: Blueprint(name, __name__), **kw
    )


@pytest.fixture
def fake_registry(monkeypatch):
    catalogue = {}

    monkeypatch.setattr(registry, "available_names", lambda: list(catalogue))
    monkeypatch.setattr(registry, "load", lambda name: catalogue.get(name))
    return catalogue


def test_install_mounts_each_plugin_under_its_own_prefix(fake_registry):
    fake_registry["gating"] = _fake_plugin("gating")
    fake_registry["roi"] = _fake_plugin("roi")
    app = FakeApp()

    installed = registry.install(app, ["gating", "roi"])

    assert [p.name for p in installed] == ["gating", "roi"]
    assert app.registered == [("gating", "/plugins/gating"), ("roi", "/plugins/roi")]


def test_several_plugins_can_be_active_at_once(fake_registry):
    """The limit this replaces: exactly one module per process, so installing a
    second plugin disabled the first."""
    fake_registry["gating"] = _fake_plugin("gating")
    fake_registry["roi"] = _fake_plugin("roi")
    app = FakeApp()

    registry.install(app, None)

    assert len(registry.installed(app)) == 2


def test_no_plugins_requested_installs_nothing(fake_registry):
    fake_registry["gating"] = _fake_plugin("gating")
    app = FakeApp()

    registry.install(app, [])

    assert registry.installed(app) == []
    assert app.registered == []


def test_unknown_names_are_skipped_rather_than_fatal(fake_registry, capsys):
    """A stale entry in PLEXORA_PLUGINS must not make the app unstartable."""
    fake_registry["gating"] = _fake_plugin("gating")
    app = FakeApp()

    registry.install(app, ["gating", "nope"])

    assert [p.name for p in registry.installed(app)] == ["gating"]
    assert "nope" in capsys.readouterr().out


def test_a_plugin_that_fails_to_load_is_skipped(fake_registry, monkeypatch):
    fake_registry["gating"] = _fake_plugin("gating")
    fake_registry["broken"] = None  # load() returns None for an unusable plugin
    app = FakeApp()

    registry.install(app, ["broken", "gating"])

    assert [p.name for p in registry.installed(app)] == ["gating"]


def test_a_plugin_with_no_blueprint_still_installs(fake_registry):
    """A plugin can be UI-only -- panels and scripts, no server routes."""
    fake_registry["viewonly"] = Plugin(name="viewonly", label="View Only")
    app = FakeApp()

    registry.install(app, ["viewonly"])

    assert [p.name for p in registry.installed(app)] == ["viewonly"]
    assert app.registered == []


def test_find_and_tools_for(fake_registry):
    fake_registry["gating"] = _fake_plugin("gating", requires=Requires(table=True))
    fake_registry["roi"] = _fake_plugin("roi")
    app = FakeApp()
    registry.install(app, None)

    assert registry.find(app, "gating").name == "gating"
    assert registry.find(app, "absent") is None

    image_only = {"image_kind": "ome_tiff"}
    assert [p.name for p in registry.tools_for(app, image_only)] == ["roi"]
    assert {p.name for p in registry.tools_for(app, FULL)} == {"gating", "roi"}


# --------------------------------------------------------------------------
# The real bundled plugin
# --------------------------------------------------------------------------

def test_gating_is_discoverable_without_being_imported():
    """Names must resolve without executing plugin code -- importing gating's
    descriptor drags in anndata and h5py, which is the cost a core build
    exists to avoid."""
    assert "gating" in registry.available_names()


def test_gating_package_name_matches_its_declared_name():
    """Discovery knows the package name before the descriptor, so the two must
    agree or the plugin is unaddressable."""
    plugin = registry.load("gating")
    assert plugin is not None and plugin.name == "gating"
