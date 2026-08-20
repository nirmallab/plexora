"""Plugin discovery, activation and applicability.

The end-to-end behaviour (a core build importing nothing, a plugin build
mounting namespaced routes) is covered by test_plugin_boundary.py, which runs
real subprocesses. This file covers the logic in isolation, including the
failure modes a third-party plugin will eventually exercise.
"""

import pytest
from flask import Blueprint

from tests.helpers import anndata_spec, csv_spec, project
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

FULL = project(dataset=csv_spec("/data/cells.csv", image_id="sample",
                                markers=["CD3"], metadata=["CellID"]),
               segmentation="/seg.tif")
NO_TABLE = project(dataset=None)
RGB = project(dataset=None, kind="rgb")


def test_a_plugin_needing_nothing_applies_to_any_real_image():
    assert Requires().satisfied_by(FULL)
    assert Requires().satisfied_by(NO_TABLE)


def test_rgb_quick_view_is_excluded_by_default():
    """The flat RGB path has no channels and no feature data, so marker tools
    are meaningless there."""
    assert not Requires().satisfied_by(RGB)


def test_table_requirement_rejects_a_project_without_one():
    """No dataset block IS the "image only" state -- there is no separate flag
    to keep in step with it, and no stub file to tell apart from a real one."""
    assert Requires(table=True).satisfied_by(FULL)
    assert not Requires(table=True).satisfied_by(NO_TABLE)


def test_segmentation_requirement():
    assert Requires(segmentation=True).satisfied_by(FULL)
    assert not Requires(segmentation=True).satisfied_by(project(dataset=None))


def test_a_pending_mask_already_counts_as_supplied():
    """Conversion runs for tens of seconds in the background. Asking again for
    a mask the user has already given would be the wrong question."""
    assert Requires(segmentation=True).satisfied_by(
        project(dataset=csv_spec("/data/cells.csv"), segmentation="pending"))


def test_role_requirements_are_reported_once_there_is_a_table():
    """Which column holds the cell id is a question with no answers until the
    columns exist, so the table requirement subsumes it."""
    requires = Requires(table=True, roles=("cell_id", "image_id"))
    assert [r.key for r in requires.missing_from(NO_TABLE)] == ["table"]

    # single_image=False leaves the image-id question genuinely open, which
    # is what this test is about -- the helper otherwise answers it, since
    # most fixtures are not here to exercise the asking machinery.
    partial = project(dataset=csv_spec("/data/cells.csv", cell_id="CellID",
                                       image_id=None, single_image=False))
    assert [r.key for r in requires.missing_from(partial)] == ["role:image_id"]

    complete = project(dataset=csv_spec("/data/cells.csv", cell_id="CellID", image_id="sample"))
    assert requires.satisfied_by(complete)


def test_the_adapters_own_id_column_does_not_count_as_a_cell_id_answer():
    """The bug this branch exists for. For AnnData and SpatialData the importer
    writes the adapter's positional "id" into `roles.cell_id` the moment a
    table loads -- it describes the table that comes out, not a choice anyone
    made. Reading the role as the answer reported every such project as having
    answered, so the question was never asked and the project kept a row-number
    cell id while its mask was labelled from an obs column: gates lit up cells
    hundreds of pixels from the ones that passed them."""
    requires = Requires(table=True, roles=("cell_id",))

    fresh = project(dataset=anndata_spec("/data/cells.h5ad", markers=["CD3"],
                                         obs_columns=["MaskLabel"],
                                         row_number_ids=False))
    assert fresh.roles.cell_id == "id"
    assert [r.key for r in requires.missing_from(fresh)] == ["role:cell_id"]

    named = fresh.with_role_answers({"cell_id": "MaskLabel"})
    assert requires.satisfied_by(named)

    # And the answer that names no column satisfies it just as fully -- without
    # that, a file with no id column could only answer by leaving the question
    # blank, which is the state this test says is not an answer.
    numbered = fresh.with_row_number_ids(True)
    assert numbered.roles.cell_id == "id"
    assert requires.satisfied_by(numbered)


def test_a_csv_cell_id_is_still_read_straight_off_the_role():
    """The distinction is specific to the formats whose table is synthesized
    from a read spec. A CSV's cell id is one of its own columns, named by the
    user on the classification screen, and there is no adapter default to
    mistake it for."""
    requires = Requires(table=True, roles=("cell_id",))

    assert requires.satisfied_by(
        project(dataset=csv_spec("/data/cells.csv", cell_id="CellID")))
    assert [r.key for r in requires.missing_from(
        project(dataset=csv_spec("/data/cells.csv", cell_id=None)))] == ["role:cell_id"]


def test_marker_classification_is_a_requirement_of_its_own():
    requires = Requires(table=True, markers=True)
    unclassified = project(dataset=csv_spec("/data/cells.csv"))
    assert [r.key for r in requires.missing_from(unclassified)] == ["markers"]
    assert requires.satisfied_by(
        project(dataset=csv_spec("/data/cells.csv", markers=["CD3"])))


def test_a_structural_split_is_never_put_up_for_confirmation():
    """A guess gets shown once; a fact does not. An AnnData or SpatialData file
    states its own split -- var is markers, obs is annotations -- so rendering
    the drag-and-drop classifier for one asks the user to confirm what the file
    already says. A CSV header states nothing, so that one still gets asked."""
    requires = Requires(table=True, markers=True)

    structural = project(dataset=anndata_spec("/data/cells.h5ad", markers=["CD3"],
                                              metadata=["id", "X", "Y"]))
    assert [r.key for r in requires.unconfirmed_from(structural)] == []

    guessed = project(dataset=csv_spec("/data/cells.csv", markers=["CD3"]))
    assert "markers" in [r.key for r in requires.unconfirmed_from(guessed)]


def test_optional_requirements_never_block_but_are_still_offered():
    """The difference that lets gating open on a project with no mask while
    still giving the user somewhere to add one."""
    requires = Requires(table=True, optional=("segmentation", "role:image_id"))
    maskless = project(dataset=csv_spec("/data/cells.csv", image_id=None,
                                        single_image=False))
    assert requires.satisfied_by(maskless)
    assert {r.key for r in requires.optional_missing_from(maskless)} == {
        "segmentation", "role:image_id"}
    assert requires.optional_missing_from(FULL) == []


def test_a_requirement_naming_an_unknown_role_is_rejected_at_declaration():
    """A typo in a plugin's Requires would otherwise become a requirement no
    answer can ever satisfy, and the tool would never open."""
    with pytest.raises(ValueError, match="unknown column role"):
        Requires(roles=("cell_ids",))
    with pytest.raises(ValueError, match="unknown"):
        Requires(optional=("role:nope",))


# --------------------------------------------------------------------------
# Applicability vs readiness -- the distinction that keeps the Tools menu
# reachable for a project that has not got its feature table yet
# --------------------------------------------------------------------------

def test_a_missing_table_is_recoverable_so_the_tool_still_applies():
    """The regression this pins: filtering the Tools menu by satisfied_by hid
    gating from every project without a feature table -- and opening the tool
    is the ONLY route to the page that attaches one, so hiding it hid the fix
    as well."""
    requires = Requires(table=True)
    assert requires.applies_to(NO_TABLE)
    assert not requires.satisfied_by(NO_TABLE)
    assert [r.key for r in requires.missing_from(NO_TABLE)] == ["table"]


def test_an_incompatible_image_kind_is_not_recoverable():
    """No upload turns a flat RGB image into something with channels, so this
    one really is hidden."""
    requires = Requires(table=True)
    assert not requires.applies_to(RGB)
    assert requires.missing_from(FULL) == []


def test_missing_from_reports_every_absent_input():
    requires = Requires(table=True, segmentation=True)
    assert [r.key for r in requires.missing_from(NO_TABLE)] == ["table", "segmentation"]
    assert requires.missing_from(FULL) == []


def test_a_requirement_describes_itself_well_enough_for_core_to_render_it():
    """Core builds the form from these without knowing which plugin asked or
    what it wants them for -- so kind and label have to carry the meaning."""
    missing = Requires(table=True, roles=("x",)).missing_from(NO_TABLE)
    assert [(r.key, r.kind) for r in missing] == [("table", "data")]
    assert missing[0].label

    role = Requires(roles=("x",)).missing_from(project(dataset=csv_spec("/c.csv", x=None)))[0]
    assert (role.kind, role.role) == ("role", "x")


def test_describing_a_role_requirement_names_the_role_it_asks_about():
    """The modal keys its answers by `role`, so it has to be in the payload --
    reading it off the Python object is not enough. While it was missing every
    role select posted under the literal key "undefined", each field clobbering
    the last, and the answers were dropped server-side while the questions were
    marked answered -- leaving a project running on roles nobody chose."""
    role = Requires(roles=("cell_id",)).missing_from(
        project(dataset=csv_spec("/c.csv", cell_id=None)))[0]

    assert role.describe()["role"] == "cell_id"


def test_describing_a_non_role_requirement_names_no_role():
    assert Requires(table=True).missing_from(NO_TABLE)[0].describe()["role"] is None


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

    # Offered even though it cannot run yet -- that is how the user gets asked
    # for the missing data. Only `ready_tools` narrows to what will open now.
    assert {p.name for p in registry.tools_for(app, NO_TABLE)} == {"gating", "roi"}
    assert [p.name for p in registry.ready_tools(app, NO_TABLE)] == ["roi"]
    assert {p.name for p in registry.ready_tools(app, FULL)} == {"gating", "roi"}


def test_an_incompatible_datasource_drops_out_of_the_menu(fake_registry):
    """Compatibility still filters: an RGB quick view offers no marker tools."""
    fake_registry["gating"] = _fake_plugin("gating", requires=Requires(table=True))
    app = FakeApp()
    registry.install(app, None)

    assert registry.tools_for(app, {"image_kind": "rgb"}) == []


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


# --------------------------------------------------------------------------
# What the requirements form posts back
# --------------------------------------------------------------------------

def test_a_role_core_cannot_store_is_not_marked_answered():
    """`_supplied_keys` and `with_role_answers` have to agree on what counts as
    a role. They did not: an unknown key was dropped from the answers but still
    recorded as confirmed, so the question stopped being asked while the answer
    was never applied -- the exact shape of the `role:undefined` bug."""
    from plexora.server.routes.tool_routes import _supplied_keys

    keys = _supplied_keys({"roles": {"cell_id": "global_cell_id",
                                     "undefined": "global_cell_id"}})

    assert keys == ["role:cell_id"]
