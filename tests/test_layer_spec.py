"""The layer model: one project, several registered layers, one coordinate system.

Plexora was a single-image viewer -- `Project.image` was the whole spatial model.
A spatial transcriptomics run is a scene instead: a morphology image, often an
H&E and a multiplex IF section registered alongside it, transcript coordinates,
and boundary polygons, all in one coordinate system.

Three properties make growing that model safe, and all three are pinned here.

**A project that predates layers is written back byte for byte.** `spatialLayers`
is omitted when empty, exactly as `resources` is. Every config.json in the world
today has no layers, and a save must not start writing `[]` into all of them.

**The reference, mask and centroid layers are synthesized, never stored.** They
come out of `ImageSpec`, `SegmentationSpec` and the table's coordinate roles, so
a layer list describes exactly what a pre-layers project already draws, at the
position it already draws it -- which is what lets the client be ported onto the
layer list with no visible change.

**A reserved id cannot be stored.** A layer written under `__image__` would be
shadowed by the synthesized one: written, never drawn, and silent about it.
"""

import pytest

from plexora.server.models.project import (
    CENTROID_LAYER_ID,
    IDENTITY_TRANSFORM,
    LAYER_KINDS,
    MASK_LAYER_ID,
    REFERENCE_LAYER_ID,
    LayerSpec,
    Project,
    normalize_transform,
)

from tests.helpers import csv_spec, project


# -- round-tripping ---------------------------------------------------------

def test_a_project_with_no_layers_writes_no_layers_key():
    """The whole back-compat story in one assertion. Every stored project has
    no layers; a save that started writing an empty list into each of them
    would be a migration nobody asked for."""
    entry = project("demo").to_entry()

    assert "spatialLayers" not in entry
    assert Project.from_entry("demo", entry).to_entry() == entry


def test_a_layer_round_trips_through_the_entry():
    original = project("demo").with_layer(LayerSpec(
        id="he",
        kind="image",
        label="H&E",
        src="/data/he.svs",
        width=4096,
        height=2048,
        max_level=5,
        transform=(1.0, 0.0, 0.0, 1.0, 500.0, -12.5),
        coordinate_system="global",
        render={"opacity": 0.6},
    ))

    entry = original.to_entry()

    assert Project.from_entry("demo", entry).to_entry() == entry
    assert entry["spatialLayers"][0]["transform"] == [1.0, 0.0, 0.0, 1.0, 500.0, -12.5]


def test_a_key_the_layer_does_not_model_survives_a_save():
    """The same rule the project itself follows, for the same reason: a writer
    that adds a key must not have it deleted by a reader that predates it."""
    stored = {
        **project("demo").to_entry(),
        "spatialLayers": [{"id": "x", "kind": "points", "somethingNew": {"a": 1}}],
    }

    saved = Project.from_entry("demo", stored).to_entry()

    assert saved["spatialLayers"][0]["somethingNew"] == {"a": 1}


def test_an_absent_transform_is_absent_rather_than_identity():
    """None and identity are different claims. A layer with no transform has
    never been registered; one storing the identity has been, and was found to
    already line up. Writing identity for both would erase the distinction and
    add six numbers to every layer entry."""
    stored = project("demo").with_layer(LayerSpec(id="x", kind="points")).to_entry()

    assert "transform" not in stored["spatialLayers"][0]
    layer = Project.from_entry("demo", stored).spatial_layers[0]
    assert layer.transform is None
    assert layer.affine == IDENTITY_TRANSFORM


def test_visible_is_written_only_when_false():
    on = project("demo").with_layer(LayerSpec(id="x", kind="points")).to_entry()
    off = project("demo").with_layer(
        LayerSpec(id="x", kind="points", visible=False)).to_entry()

    assert "visible" not in on["spatialLayers"][0]
    assert off["spatialLayers"][0]["visible"] is False
    assert Project.from_entry("demo", off).spatial_layers[0].visible is False


# -- the transform ----------------------------------------------------------

@pytest.mark.parametrize("raw", [
    None, (), (1, 0, 0, 1, 0), (1, 0, 0, 1, 0, 0, 0), "nope",
    (1, 0, 0, 1, 0, "x"), (1, 0, 0, 1, 0, float("nan")), (1, 0, 0, 1, 0, float("inf")),
])
def test_an_unusable_transform_is_dropped_rather_than_repaired(raw):
    """Six finite numbers or nothing. A five-element transform is not a
    transform missing one number -- it is a writer that did not know the shape,
    and drawing with a guessed sixth would put the layer somewhere nobody
    asked for and report success."""
    assert normalize_transform(raw) is None


def test_the_transform_is_stored_in_canvas_order():
    """[a, b, c, d, e, f] as ctx.transform() and SVG's matrix() take them, not a
    numpy 2x3. The client's hot path applies it per overlay per frame, and a
    conversion there would run every one of those frames."""
    layer = LayerSpec.from_entry({"id": "x", "transform": [2, 0, 0, 3, 10, 20]})

    a, b, c, d, e, f = layer.affine
    # x' = a*x + c*y + e, y' = b*x + d*y + f
    assert (a * 1 + c * 0 + e, b * 1 + d * 0 + f) == (12, 20)
    assert (a * 0 + c * 1 + e, b * 0 + d * 1 + f) == (10, 23)


# -- the synthesized layers -------------------------------------------------

def test_a_plain_image_project_has_one_layer():
    layers = project("demo").all_layers

    assert [layer.id for layer in layers] == [REFERENCE_LAYER_ID]
    assert layers[0].kind == "image"
    assert layers[0].transform is None, "the reference layer IS the coordinate system"


def test_a_mask_becomes_a_labels_layer():
    layers = project("demo", segmentation="/seg.tif").all_layers

    assert [layer.id for layer in layers] == [REFERENCE_LAYER_ID, MASK_LAYER_ID]
    assert layers[1].kind == "labels"


def test_a_pending_mask_is_not_a_layer_yet():
    """`available`, not `requested`: a mask whose pyramid is still building has
    no tiles to serve, and a layer card for it would offer a control that draws
    nothing."""
    layers = project("demo", segmentation="pending").all_layers

    assert [layer.id for layer in layers] == [REFERENCE_LAYER_ID]


def test_a_table_with_coordinates_becomes_a_points_layer():
    layers = project("demo", dataset=csv_spec("/cells.csv")).all_layers

    assert [layer.id for layer in layers] == [REFERENCE_LAYER_ID, CENTROID_LAYER_ID]
    assert layers[1].kind == "points"


def test_a_table_with_no_coordinates_is_not_a_points_layer():
    """Centroids are positions. A table whose x/y columns nobody has identified
    yet has none, which is the same as having no table at all here."""
    spec = csv_spec("/cells.csv")
    layers = project("demo", dataset=spec.__class__(
        **{**spec.__dict__, "roles": spec.roles.with_values({"x": None, "y": None})}
    )).all_layers

    assert [layer.id for layer in layers] == [REFERENCE_LAYER_ID]


def test_registered_layers_come_after_the_synthesized_ones():
    """Bottom of the stack first, and the image is the bottom. A registered
    layer defaults to sitting above what the project already drew."""
    p = project("demo", segmentation="/seg.tif").with_layer(
        LayerSpec(id="transcripts", kind="points"))

    assert [layer.id for layer in p.all_layers] == [
        REFERENCE_LAYER_ID, MASK_LAYER_ID, "transcripts"]


def test_the_reference_layer_carries_the_image_binding():
    """A layer's remoteness rides on its own binding rather than on
    RESOURCE_KINDS, which stays the closed three-tuple every dispatch guard in
    data_model tests."""
    from plexora.server.models.project import ResourceBinding

    binding = ResourceBinding(kind="image", provider="node", node="hpc", resource_id="img1")
    p = project("demo").patch(resources={"image": binding})

    assert p.reference_layer.binding is binding
    assert p.reference_layer.is_remote


# -- reserved ids -----------------------------------------------------------

@pytest.mark.parametrize("bad", [REFERENCE_LAYER_ID, MASK_LAYER_ID, CENTROID_LAYER_ID])
def test_a_reserved_id_cannot_be_registered(bad):
    with pytest.raises(ValueError):
        project("demo").with_layer(LayerSpec(id=bad, kind="image"))


@pytest.mark.parametrize("bad", [REFERENCE_LAYER_ID, MASK_LAYER_ID, CENTROID_LAYER_ID])
def test_a_reserved_id_already_on_disk_is_dropped_on_read(bad):
    """Repairing it would mean renaming somebody's layer. Dropping it is what
    makes the config file wrong in a way that shows."""
    stored = {**project("demo").to_entry(), "spatialLayers": [{"id": bad, "kind": "image"}]}

    assert Project.from_entry("demo", stored).spatial_layers == ()


def test_a_layer_with_no_id_is_dropped_on_read():
    stored = {**project("demo").to_entry(), "spatialLayers": [{"kind": "image"}]}

    assert Project.from_entry("demo", stored).spatial_layers == ()


def test_a_duplicate_id_keeps_the_first():
    stored = {**project("demo").to_entry(),
              "spatialLayers": [{"id": "x", "label": "first"},
                                {"id": "x", "label": "second"}]}

    layers = Project.from_entry("demo", stored).spatial_layers

    assert [layer.label for layer in layers] == ["first"]


# -- editing ----------------------------------------------------------------

def test_replacing_a_layer_keeps_its_position():
    """Re-registering a layer with a corrected transform must not move it to
    the top of the stack -- the order is the user's, and they did not ask."""
    p = (project("demo")
         .with_layer(LayerSpec(id="a", kind="image"))
         .with_layer(LayerSpec(id="b", kind="points"))
         .with_layer(LayerSpec(id="c", kind="shapes")))

    p = p.with_layer(LayerSpec(id="b", kind="points", label="renamed"))

    assert [layer.id for layer in p.spatial_layers] == ["a", "b", "c"]
    assert p.layer("b").label == "renamed"


def test_removing_a_layer_leaves_the_others():
    p = (project("demo")
         .with_layer(LayerSpec(id="a", kind="image"))
         .with_layer(LayerSpec(id="b", kind="points")))

    assert [layer.id for layer in p.without_layer("a").spatial_layers] == ["b"]


def test_removing_a_synthesized_layer_does_nothing():
    """The way to remove the mask layer is to remove the mask. Silently
    accepting the request and changing nothing is the honest answer; raising
    would make a Layer Manager's remove button a special case per row."""
    p = project("demo", segmentation="/seg.tif")

    assert p.without_layer(MASK_LAYER_ID).to_entry() == p.to_entry()


def test_an_unknown_kind_falls_back_to_image():
    """Every kind names a renderer. A kind with no renderer draws nothing at
    all, so the one that at least shows the layer exists is the better guess --
    and LAYER_KINDS is the list that has to grow for a real new one."""
    layer = LayerSpec.from_entry({"id": "x", "kind": "hologram"})

    assert layer.kind == "image"
    assert layer.kind in LAYER_KINDS


# -- restacking ------------------------------------------------------------

def test_the_order_is_stored_bottom_first():
    """Which slide is on top of which is a statement about the SAMPLE, so it
    is stored. Bottom first, because that is the order `all_layers` returns
    and the order the client's LayerStack holds."""
    scene = (project("demo")
               .with_layer(LayerSpec(id="a", kind="image"))
               .with_layer(LayerSpec(id="b", kind="image"))
               .with_layer(LayerSpec(id="c", kind="image")))
    assert [l.id for l in scene.with_layer_order(["c", "a", "b"]).spatial_layers] \
        == ["c", "a", "b"]


def test_an_unmentioned_layer_keeps_its_place_underneath():
    """The same partial-order rule the client's `LayerStack.setOrder` follows.
    The two have to agree: the Layers panel sends the order it is SHOWING, and
    a stored order that dropped what it has no card for would lose it on the
    round trip."""
    scene = (project("demo")
               .with_layer(LayerSpec(id="a", kind="image"))
               .with_layer(LayerSpec(id="b", kind="image"))
               .with_layer(LayerSpec(id="c", kind="image")))
    assert [l.id for l in scene.with_layer_order(["a"]).spatial_layers] \
        == ["b", "c", "a"]


def test_ordering_a_synthesized_layer_is_refused():
    """Where the mask and the centroids composite is `all_layers`' answer
    every time it is read -- not something to store. A caller naming one
    believes it can move something it cannot, and a silent no-op would leave it
    believing that.

    The reference image is the exception, and has its own tests below."""
    scene = project("demo").with_layer(LayerSpec(id="a", kind="image"))
    with pytest.raises(ValueError):
        scene.with_layer_order(["__mask__", "a"])
    with pytest.raises(ValueError):
        scene.with_layer_order(["__centroids__", "a"])


# -- the reference image's own place ---------------------------------------

def _three():
    return (project("demo")
            .with_layer(LayerSpec(id="a", kind="image"))
            .with_layer(LayerSpec(id="b", kind="image"))
            .with_layer(LayerSpec(id="c", kind="image")))


def test_the_reference_image_starts_at_the_bottom():
    """Where it has always been, and where all but a handful of projects will
    leave it. Nothing in a config.json says so."""
    scene = _three()
    assert scene.image_depth == 0
    assert [l.id for l in scene.all_layers] == ["__image__", "a", "b", "c"]
    assert "imageDepth" not in scene.to_entry()


def test_the_reference_image_can_be_ordered_with_the_rest():
    """Which image was imported first should not decide which one can be drawn
    on top: a project whose reference is the multiplex and whose H&E arrived as
    a layer is the same scene as one imported the other way round."""
    scene = _three().with_layer_order(["a", "__image__", "b", "c"])
    assert [l.id for l in scene.all_layers] == ["a", "__image__", "b", "c"]
    assert scene.image_depth == 1

    top = _three().with_layer_order(["a", "b", "c", "__image__"])
    assert [l.id for l in top.all_layers] == ["a", "b", "c", "__image__"]


def test_the_reference_image_keeps_its_depth_when_it_is_not_named():
    """The same partial-order rule everything else here follows: an id the
    caller did not mention keeps what it had. A panel that reordered two
    registered layers must not drop the image back to the floor."""
    scene = _three().with_layer_order(["a", "b", "c", "__image__"])
    kept = scene.with_layer_order(["c", "b", "a"])
    assert kept.image_depth == 3
    assert [l.id for l in kept.all_layers] == ["c", "b", "a", "__image__"]


def test_the_reference_image_is_named_at_most_once():
    with pytest.raises(ValueError):
        _three().with_layer_order(["a", "__image__", "b", "__image__"])


def test_a_depth_that_outlived_its_layers_is_clamped():
    """A stored index into a list that can shrink. Removing the layer the
    image was dragged above must not put the image off the end of the stack --
    too large can only mean "on top", which is what was last asked for."""
    scene = _three().with_layer_order(["a", "b", "c", "__image__"])
    shrunk = scene.without_layer("c").without_layer("b")
    assert [l.id for l in shrunk.all_layers] == ["a", "__image__"]


def test_an_unreadable_depth_reads_as_the_ground():
    from plexora.server.models.project import Project
    entry = dict(project("demo").to_entry(), imageDepth="not a number")
    assert Project.from_entry("demo", entry).image_depth == 0


def test_the_reference_image_carries_a_stored_render():
    """It is synthesized from `ImageSpec` rather than stored, so it had nowhere
    to put an answer to a question about presentation. It needed one the moment
    the image stopped being the one layer with nothing underneath it: a ground
    is only worth naming when it can be seen."""
    from plexora.server.models.project import Project
    scene = project("demo").patch(image_render={"background": "#ffffff"})
    assert scene.reference_layer.render["background"] == "#ffffff"
    assert scene.to_entry()["imageRender"] == {"background": "#ffffff"}
    assert Project.from_entry("demo", scene.to_entry()).image_render \
        == {"background": "#ffffff"}


def test_the_image_kind_is_not_the_users_to_overwrite():
    """It is read off the file. A stored one would freeze today's answer and
    outlive a re-import that corrected it."""
    scene = project("demo").patch(image_render={"imageKind": "nonsense"})
    assert scene.reference_layer.render["imageKind"] != "nonsense"


def test_ordering_an_unknown_layer_is_refused():
    scene = project("demo").with_layer(LayerSpec(id="a", kind="image"))
    with pytest.raises(ValueError):
        scene.with_layer_order(["ghost"])


def test_an_empty_order_changes_nothing():
    scene = (project("demo")
               .with_layer(LayerSpec(id="a", kind="image"))
               .with_layer(LayerSpec(id="b", kind="image")))
    assert [l.id for l in scene.with_layer_order([]).spatial_layers] == ["a", "b"]
