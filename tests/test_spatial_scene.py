"""What a spatial store holds, and where each piece sits.

Deliberately the LAST thing built. By the time an importer runs, every consumer
of a layer already exists and is tested -- the model, the transform maths, the
Layer Manager, the plugin API, the tile route -- so this fills in a shape that is
already proven rather than defining one.

Three properties are pinned, and the first two are where a silent wrong answer
would come from.

**Registration is inverse(reference) after element.** Both elements say where
they land in a shared system; composing one through the other's inverse is the
whole of it, and it is why the shared system never has to be the one the viewer
draws in. Getting the order backwards produces a transform that is plausible and
wrong, and the picture still looks like a picture.

**An element that does not reach the shared system is NOT aligned.** It gets
None, and the Layer Manager says "aligned by assumption". Returning the identity
instead would claim a registration nobody performed -- which is exactly what
Plexora did everywhere before this.

**A Xenium run's pixel size is read, never assumed.** 0.2125 is the published
value for the current instrument. A run from another instrument states its own,
and a viewer that assumed one would put every transcript at the wrong distance
from the origin while looking entirely right.
"""

import json

import pytest

from plexora.server.models.project import (
    REFERENCE_LAYER_ID,
    ImageSpec,
    Project,
)
from plexora.server.utils import spatial_scene

from tests.spatial_fixtures import (
    anisotropic,
    rotation,
    scale,
    shear,
    translation,
    write_spatialdata_store,
)


# -- recognising a store ----------------------------------------------------

def test_a_spatialdata_store_is_recognised_by_its_element_groups(tmp_path):
    store = write_spatialdata_store(tmp_path / "run.zarr")

    assert spatial_scene.is_spatialdata_store(store)
    assert not spatial_scene.is_xenium_run(store)


def test_an_ordinary_directory_is_neither(tmp_path):
    (tmp_path / "plain").mkdir()

    assert not spatial_scene.is_spatialdata_store(tmp_path / "plain")
    assert not spatial_scene.is_xenium_run(tmp_path / "plain")
    assert spatial_scene.read_scene(tmp_path / "plain") == []


def test_a_xenium_run_is_recognised_by_its_manifest(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "experiment.xenium").write_text("{}", encoding="utf-8")

    assert spatial_scene.is_xenium_run(run)


def test_a_xenium_run_is_recognised_without_its_manifest(tmp_path):
    """A user who copied three files out of a run still has a Xenium run as far
    as this is concerned. Refusing it for a missing manifest would be pedantry
    with a real cost."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "morphology.ome.tif").write_bytes(b"x")
    (run / "transcripts.parquet").write_bytes(b"x")

    assert spatial_scene.is_xenium_run(run)


def test_one_lonely_xenium_file_is_not_a_run(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "transcripts.parquet").write_bytes(b"x")

    assert not spatial_scene.is_xenium_run(run)


# -- enumerating a SpatialData store ---------------------------------------

def test_every_element_group_becomes_the_kind_that_draws_it(tmp_path):
    store = write_spatialdata_store(tmp_path / "run.zarr", elements={
        "images/morphology": [scale(0.2125, 0.2125)],
        "labels/cells": [scale(0.2125, 0.2125)],
        "points/transcripts": [scale(1, 1)],
        "shapes/cell_boundaries": [scale(1, 1)],
    })

    kinds = {e.id: e.kind for e in spatial_scene.read_spatialdata_scene(store)}

    assert kinds == {
        "images/morphology": "image",
        "labels/cells": "labels",
        "points/transcripts": "points",
        "shapes/cell_boundaries": "shapes",
    }


def test_the_element_order_is_stable(tmp_path):
    """Two reads of one store must give the same order, or a re-import silently
    restacks somebody's scene."""
    store = write_spatialdata_store(tmp_path / "run.zarr", elements={
        "images/b": [], "images/a": [], "points/z": [], "points/y": [],
    })

    first = [e.id for e in spatial_scene.read_spatialdata_scene(store)]
    second = [e.id for e in spatial_scene.read_spatialdata_scene(store)]

    assert first == second == ["images/a", "images/b", "points/y", "points/z"]


def test_the_reference_is_the_first_image(tmp_path):
    store = write_spatialdata_store(tmp_path / "run.zarr", elements={
        "points/transcripts": [], "images/morphology": [], "images/he": [],
    })

    elements = spatial_scene.read_spatialdata_scene(store)

    assert spatial_scene.reference_of(elements).id == "images/he"


def test_a_scene_with_no_image_has_no_reference(tmp_path):
    """Honest, and the state a pure-transcript sample is genuinely in."""
    store = write_spatialdata_store(tmp_path / "run.zarr",
                                    elements={"points/transcripts": []})

    assert spatial_scene.reference_of(spatial_scene.read_spatialdata_scene(store)) is None


# -- registration -----------------------------------------------------------

def test_an_element_is_registered_through_the_reference(tmp_path):
    """The reference maps its own pixel (1, 1) to global (2, 2); the layer sits
    at global (20, 10). So the layer is at reference pixel (10, 5)."""
    store = write_spatialdata_store(tmp_path / "run.zarr", elements={
        "images/morphology": [scale(2, 2)],
        "points/transcripts": [translation(10, 20)],
    })

    layers = spatial_scene.layers_for(spatial_scene.read_spatialdata_scene(store))
    transcripts = next(layer for layer in layers if layer.id == "points/transcripts")

    a, b, c, d, e, f = transcripts.transform
    assert (e, f) == pytest.approx((10.0, 5.0))


def test_the_reference_itself_is_not_a_registered_layer(tmp_path):
    """Project.reference_layer synthesizes it from ImageSpec. A second copy
    under its own id would be a layer written and never drawn."""
    store = write_spatialdata_store(tmp_path / "run.zarr", elements={
        "images/morphology": [scale(2, 2)],
        "points/transcripts": [translation(1, 1)],
    })

    layers = spatial_scene.layers_for(spatial_scene.read_spatialdata_scene(store))

    assert [layer.id for layer in layers] == ["points/transcripts"]


def test_an_element_in_another_coordinate_system_is_not_registered(tmp_path):
    """None, not identity. It means nobody registered these against each other,
    and the Layer Manager says so -- which is the thing the viewer never used to
    say."""
    store = write_spatialdata_store(tmp_path / "run.zarr", elements={
        "images/morphology": [scale(2, 2, output="global")],
        "points/transcripts": [translation(1, 1, output="aligned")],
    })

    layers = spatial_scene.layers_for(spatial_scene.read_spatialdata_scene(store))
    transcripts = next(layer for layer in layers if layer.id == "points/transcripts")

    assert transcripts.transform is None
    assert transcripts.coordinate_system is None


def test_a_rotation_written_as_an_affine_survives(tmp_path):
    """NGFF has no rotation type, so every writer emits one as an affine -- which
    is why the reader handles `affine` at all, and OSD handles the result
    exactly."""
    store = write_spatialdata_store(tmp_path / "run.zarr", elements={
        "images/morphology": [],
        "images/he": [rotation(7)],
    })

    layers = spatial_scene.layers_for(spatial_scene.read_spatialdata_scene(store))
    he = next(layer for layer in layers if layer.id == "images/morphology")

    from plexora.server.utils.ngff_transform import decompose, unsupported_reason

    assert unsupported_reason(he.transform) is None


def test_a_sheared_element_is_recorded_and_refused_downstream(tmp_path):
    """Stored as read, and refused where it would be drawn. The importer's job
    is to say what the file says; the viewer's is to refuse what it cannot draw
    without approximating it."""
    from plexora.server.utils.ngff_transform import unsupported_reason

    store = write_spatialdata_store(tmp_path / "run.zarr", elements={
        "images/morphology": [],
        "images/skewed": [shear(0.5)],
    })

    layers = spatial_scene.layers_for(spatial_scene.read_spatialdata_scene(store))
    skewed = next(layer for layer in layers if layer.id == "images/skewed")

    assert skewed.transform is not None
    assert unsupported_reason(skewed.transform) == "shear"


def test_an_anisotropic_element_is_recorded_and_refused_downstream(tmp_path):
    from plexora.server.utils.ngff_transform import unsupported_reason

    store = write_spatialdata_store(tmp_path / "run.zarr", elements={
        "images/morphology": [],
        "images/stretched": [anisotropic(1.0, 2.0)],
    })

    layers = spatial_scene.layers_for(spatial_scene.read_spatialdata_scene(store))
    stretched = next(layer for layer in layers if layer.id == "images/stretched")

    assert unsupported_reason(stretched.transform) == "anisotropic"


# -- Xenium -----------------------------------------------------------------

def _xenium(tmp_path, pixel_size=0.2125, files=None, name="run"):
    run = tmp_path / name
    run.mkdir(parents=True)
    (run / "experiment.xenium").write_text(
        json.dumps({"pixel_size": pixel_size}), encoding="utf-8")
    for name in (files or ("morphology.ome.tif", "transcripts.parquet",
                           "cell_boundaries.parquet", "nucleus_boundaries.parquet")):
        (run / name).write_bytes(b"x")
    return run


def test_a_xenium_run_enumerates_its_four_outputs(tmp_path):
    elements = spatial_scene.read_xenium_scene(_xenium(tmp_path))

    assert [(e.id, e.kind) for e in elements] == [
        ("morphology", "image"),
        ("cell_boundaries", "shapes"),
        ("nucleus_boundaries", "shapes"),
        ("transcripts", "points"),
    ]


def test_the_pixel_size_is_read_from_the_run(tmp_path):
    assert spatial_scene.xenium_pixel_size(_xenium(tmp_path, 0.2125)) == pytest.approx(0.2125)
    assert spatial_scene.xenium_pixel_size(_xenium(tmp_path, 0.425, name="other")) \
        == pytest.approx(0.425)


def test_a_run_with_no_manifest_states_no_pixel_size(tmp_path):
    """None rather than the published default. A viewer that assumed 0.2125
    would put every transcript at the wrong distance from the origin, and the
    picture would look entirely right."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "morphology.ome.tif").write_bytes(b"x")
    (run / "transcripts.parquet").write_bytes(b"x")

    assert spatial_scene.xenium_pixel_size(run) is None


def test_a_xenium_layer_is_registered_by_its_pixel_size(tmp_path):
    """Everything in a run is in microns in one frame, so the registration IS
    the conversion into the morphology image's pixel grid."""
    elements = spatial_scene.read_xenium_scene(_xenium(tmp_path))

    layers = spatial_scene.layers_for(elements, pixel_size=0.2125)
    transcripts = next(layer for layer in layers if layer.id == "transcripts")

    a, b, c, d, e, f = transcripts.transform
    assert a == pytest.approx(1 / 0.2125)
    assert d == pytest.approx(1 / 0.2125)
    assert (e, f) == (0.0, 0.0)


def test_a_broken_manifest_is_not_an_exception(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "experiment.xenium").write_text("{ not json", encoding="utf-8")

    assert spatial_scene.xenium_pixel_size(run) is None


# -- registering onto a project ---------------------------------------------

def test_registering_a_scene_adds_every_element_but_the_reference(tmp_path):
    store = write_spatialdata_store(tmp_path / "run.zarr", elements={
        "images/morphology": [scale(2, 2)],
        "points/transcripts": [translation(10, 20)],
        "shapes/cells": [scale(2, 2)],
    })
    project = Project(name="demo", image=ImageSpec(src="/x.tif", width=10, height=10))

    updated = spatial_scene.register_scene(project, store)

    assert [layer.id for layer in updated.all_layers] == [
        REFERENCE_LAYER_ID, "points/transcripts", "shapes/cells"]


def test_registering_twice_does_not_restack_the_scene(tmp_path):
    """with_layer keeps a layer's position when it already exists, so a
    re-import after a corrected pixel size leaves the user's order alone."""
    store = write_spatialdata_store(tmp_path / "run.zarr", elements={
        "images/morphology": [],
        "points/transcripts": [],
        "shapes/cells": [],
    })
    project = Project(name="demo", image=ImageSpec(src="/x.tif"))

    once = spatial_scene.register_scene(project, store)
    twice = spatial_scene.register_scene(once, store)

    assert [layer.id for layer in once.spatial_layers] \
        == [layer.id for layer in twice.spatial_layers]


def test_registering_something_that_is_not_a_scene_changes_nothing(tmp_path):
    (tmp_path / "plain").mkdir()
    project = Project(name="demo", image=ImageSpec(src="/x.tif"))

    assert spatial_scene.register_scene(project, tmp_path / "plain") is project


def test_a_registered_scene_round_trips_through_the_config(tmp_path):
    """The end of the loop: a store enumerated, registered, written and read back
    with every transform intact."""
    store = write_spatialdata_store(tmp_path / "run.zarr", elements={
        "images/morphology": [scale(2, 2)],
        "points/transcripts": [translation(10, 20)],
    })
    project = spatial_scene.register_scene(
        Project(name="demo", image=ImageSpec(src="/x.tif")), store)

    entry = project.to_entry()
    reloaded = Project.from_entry("demo", entry)

    assert reloaded.to_entry() == entry
    assert reloaded.layer("points/transcripts").transform \
        == project.layer("points/transcripts").transform
