"""Where a layer sits, read out of what the file says.

Plexora asserted the identity everywhere, in three independent places:
`ome_zarr.physical_metadata` read `type == "scale"` and dropped the translation,
`spatialdata_adapter` said a store needing a transform "would have to grow
explicit support here", and the ROI adapter hardcoded `Identity()`. For a single
image that is harmless -- the image IS the coordinate system. For a scene it is
the whole problem.

Three properties are pinned here.

**Translation survives.** It is the case every existing reader threw away, and
the one that matters most: a layer starting at (500, 0) drawn at the origin looks
exactly like a registration that failed.

**Absent is not identity.** An element that does not reach the shared coordinate
system gets None, and the viewer says "aligned by assumption" instead of claiming
a registration nobody performed.

**One affine, two implementations, one table.** ngff_transform decides where a
layer is registered; layerStack.js decides where it is drawn. Both run over
tests/golden/transform_cases.json, so neither can drift without the other.
"""

import json
import math
from pathlib import Path

import pytest

from plexora.server.utils import ngff_transform as T

TABLE = json.loads(
    (Path(__file__).parent / "golden" / "transform_cases.json").read_text(encoding="utf-8"))
CASES = TABLE["cases"]
IDS = [case["name"] for case in CASES]


# -- the shared table -------------------------------------------------------

def test_the_table_states_the_tolerance_this_module_uses():
    """One number, or a layer is registered by the server and refused by the
    viewer -- and the only symptom is a layer that never appears."""
    assert TABLE["tolerance"] == pytest.approx(T.TOLERANCE, rel=0, abs=1e-12)


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_points_land_where_the_table_says(case):
    for point, expected in zip(case["points"], case["mapped"]):
        got = T.apply(case["transform"], *point)
        assert got == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_the_refusal_matches_the_table(case):
    assert T.unsupported_reason(case["transform"]) == case["unsupported"]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_the_inverse_round_trips(case):
    """Needed on every hover: a pointer arrives in the reference layer's space
    and has to be put back into the layer's own to be hit-tested."""
    inverse = T.invert(case["transform"])
    if case["unsupported"] == "degenerate":
        assert inverse is None
        return
    for point in case["points"]:
        there = T.apply(case["transform"], *point)
        back = T.apply(inverse, *there)
        assert back == pytest.approx(point, abs=1e-9)


# -- composition ------------------------------------------------------------

def test_compose_applies_the_inner_transform_first():
    """The order that trips people up. `compose(outer, inner)` means inner then
    outer, which is matrix order -- and registration reads
    `inverse(reference->global)` AFTER `layer->global`."""
    scale = (2.0, 0.0, 0.0, 2.0, 0.0, 0.0)
    shift = (1.0, 0.0, 0.0, 1.0, 10.0, 0.0)

    assert T.apply(T.compose(scale, shift), 1, 0) == pytest.approx((22.0, 0.0))
    assert T.apply(T.compose(shift, scale), 1, 0) == pytest.approx((12.0, 0.0))


def test_a_transform_composed_with_its_inverse_is_the_identity():
    t = (1.5, 0.3, -0.2, 0.9, 12.0, -4.0)

    assert T.compose(T.invert(t), t) == pytest.approx(T.IDENTITY, abs=1e-12)


# -- NGFF coordinateTransformations -----------------------------------------

def test_a_translation_survives():
    """The one every existing reader dropped."""
    affine = T.transform_list([{"type": "translation", "translation": [10, 20]}], ["y", "x"])

    assert T.apply(affine, 0, 0) == pytest.approx((20.0, 10.0))


def test_the_axis_order_is_read_rather_than_assumed():
    """NGFF orders axes slowest-first (t, c, z, y, x), so a `scale` array is in
    that order too. Picking the wrong entry silently scales a layer by a channel
    count, which looks like a broken image rather than a misread axis."""
    values = {"type": "scale", "scale": [1, 1, 1, 2, 4]}

    affine = T.transform_list([values], ["t", "c", "z", "y", "x"])

    assert T.apply(affine, 1, 1) == pytest.approx((4.0, 2.0))


def test_transformations_apply_in_list_order():
    """The spec's rule: the first entry acts on the array coordinates and each
    later one acts on the previous result. Backwards turns a scale-then-shift
    into a shift-then-scale, which is wrong by the scale factor."""
    affine = T.transform_list([
        {"type": "scale", "scale": [2, 2]},
        {"type": "translation", "translation": [5, 5]},
    ], ["y", "x"])

    assert T.apply(affine, 1, 1) == pytest.approx((7.0, 7.0))


def test_an_unknown_transform_type_is_skipped_rather_than_guessed():
    """A type this module cannot evaluate is a claim it cannot honour. Treating
    it as the identity would place the layer somewhere nobody asked for and
    report success."""
    affine = T.transform_list([
        {"type": "sequence", "transformations": [{"type": "scale", "scale": [9, 9]}]},
    ], ["y", "x"])

    assert affine == T.IDENTITY


def test_an_ngff_affine_is_read_out_of_its_rows():
    """NGFF writes ndim rows of ndim+1 columns. A 2-D store's rows are in axis
    order, so the x row is the one the x axis names."""
    angle = math.radians(30)
    rows = [
        [math.cos(angle), -math.sin(angle), 5],   # y row
        [math.sin(angle), math.cos(angle), 7],    # x row
    ]

    affine = T.transform_list([{"type": "affine", "affine": rows}], ["y", "x"])

    assert T.apply(affine, 0, 0) == pytest.approx((7.0, 5.0))
    assert T.decompose(affine)["shear"] == pytest.approx(0.0, abs=1e-12)
    assert abs(T.decompose(affine)["rotation"]) == pytest.approx(30.0)


def test_a_flattened_affine_is_reshaped():
    affine = T.transform_list(
        [{"type": "affine", "affine": [1, 0, 3, 0, 1, 4]}], ["y", "x"])

    assert T.apply(affine, 0, 0) == pytest.approx((4.0, 3.0))


def test_a_store_with_no_xy_axes_has_no_transform():
    assert T.transform_list([{"type": "scale", "scale": [2, 2]}], ["z", "c"]) == T.IDENTITY


# -- SpatialData coordinate systems -----------------------------------------

def _element(system="global", transforms=None, axes=("y", "x")):
    return {
        "axes": [{"name": name} for name in axes],
        "coordinateTransformations": [
            {**t, "output": {"name": system}} for t in (transforms or [])
        ],
    }


def test_an_element_is_read_into_the_system_it_names():
    attrs = _element("global", [{"type": "translation", "translation": [3, 4]}])

    assert T.apply(T.element_transform(attrs, "global"), 0, 0) == pytest.approx((4.0, 3.0))


def test_an_element_that_does_not_reach_the_system_has_no_transform():
    """Information, not a failure. It means the two elements are not registered
    against each other, and saying so beats returning the identity and calling
    them aligned."""
    attrs = _element("aligned", [{"type": "translation", "translation": [3, 4]}])

    assert T.element_transform(attrs, "global") is None


def test_the_right_system_is_picked_out_of_several():
    attrs = {
        "axes": [{"name": "y"}, {"name": "x"}],
        "coordinateTransformations": [
            {"type": "translation", "translation": [1, 1], "output": {"name": "global"}},
            {"type": "translation", "translation": [9, 9], "output": {"name": "aligned"}},
        ],
    }

    assert T.apply(T.element_transform(attrs, "aligned"), 0, 0) == pytest.approx((9.0, 9.0))


def test_a_mapping_of_systems_is_read_too():
    """The shape spatialdata writes when an element lands in several systems."""
    attrs = {
        "axes": [{"name": "y"}, {"name": "x"}],
        "spatialdata_attrs": {
            "transform": {"global": [{"type": "scale", "scale": [2, 3]}]},
        },
    }

    assert T.apply(T.element_transform(attrs, "global"), 1, 1) == pytest.approx((3.0, 2.0))


def test_a_plain_ngff_image_with_no_output_is_still_read():
    """Not every store is a SpatialData store. An NGFF image names no output
    coordinate system at all, and its transformations apply unconditionally."""
    attrs = {
        "multiscales": [{
            "axes": [{"name": "y"}, {"name": "x"}],
            "coordinateTransformations": [{"type": "scale", "scale": [0.5, 0.5]}],
        }],
    }

    assert T.apply(T.element_transform(attrs), 4, 4) == pytest.approx((2.0, 2.0))


def test_registration_is_the_layer_through_the_reference_backwards():
    """The whole of it: inverse(reference -> global) after (layer -> global).
    Which is why the shared system never has to be the one the viewer draws in --
    it only has to be one both elements name."""
    reference = _element("global", [{"type": "scale", "scale": [2, 2]}])
    layer = _element("global", [{"type": "translation", "translation": [10, 20]}])

    affine = T.layer_transform(reference, layer, "global")

    # The layer sits at (20, 10) in global units; the reference maps its own
    # pixel (1, 1) to global (2, 2), so global (20, 10) is reference pixel (10, 5).
    assert T.apply(affine, 0, 0) == pytest.approx((10.0, 5.0))


def test_registration_against_a_reference_in_another_system_is_refused():
    reference = _element("aligned", [{"type": "scale", "scale": [2, 2]}])
    layer = _element("global", [{"type": "translation", "translation": [1, 1]}])

    assert T.layer_transform(reference, layer, "global") is None


def test_registration_against_a_degenerate_reference_is_refused():
    reference = _element("global", [{"type": "scale", "scale": [0, 0]}])
    layer = _element("global", [{"type": "translation", "translation": [1, 1]}])

    assert T.layer_transform(reference, layer, "global") is None


# -- reading a store off disk -----------------------------------------------

def test_zarr_v2_attributes_are_read(tmp_path):
    node = tmp_path / "image"
    node.mkdir()
    (node / ".zattrs").write_text(json.dumps({"hello": 1}), encoding="utf-8")

    assert T.read_attrs(node) == {"hello": 1}


def test_zarr_v3_attributes_are_read(tmp_path):
    node = tmp_path / "image"
    node.mkdir()
    (node / "zarr.json").write_text(
        json.dumps({"zarr_format": 3, "attributes": {"hello": 2}}), encoding="utf-8")

    assert T.read_attrs(node) == {"hello": 2}


def test_a_node_with_neither_has_nothing_to_say(tmp_path):
    """{} rather than an error: a node with no attributes is a node with no
    attributes, and every caller here already treats that as "no transform"."""
    node = tmp_path / "image"
    node.mkdir()

    assert T.read_attrs(node) == {}


def test_unreadable_json_is_not_an_exception(tmp_path):
    node = tmp_path / "image"
    node.mkdir()
    (node / ".zattrs").write_text("{ not json", encoding="utf-8")

    assert T.read_attrs(node) == {}


# -- the pyramid side -------------------------------------------------------

def test_a_pyramids_transform_keeps_the_translation_physical_metadata_drops():
    """The two exist side by side on purpose. `physical_metadata` answers "how
    big is a pixel" for the scale bar and is deliberately untouched; this
    answers "where does this store start", which registration needs and the
    scale bar does not."""
    from plexora.server.utils.ome_zarr import physical_metadata, pyramid_transform

    class Pyramid:
        multiscale = {
            "axes": [{"name": "y", "unit": "micrometer"}, {"name": "x", "unit": "micrometer"}],
            "datasets": [{"path": "0",
                          "coordinateTransformations": [{"type": "scale", "scale": [0.5, 0.5]}]}],
            "coordinateTransformations": [{"type": "translation", "translation": [100, 200]}],
        }

    pyramid = Pyramid()

    assert pyramid_transform(pyramid) == pytest.approx((0.5, 0, 0, 0.5, 200, 100))
    assert physical_metadata(pyramid)["physical_size_x"] == pytest.approx(0.5)


def test_a_pyramid_naming_no_xy_axes_has_no_transform():
    from plexora.server.utils.ome_zarr import pyramid_transform

    assert pyramid_transform(type("P", (), {"multiscale": {}})()) is None


# -- decomposition ----------------------------------------------------------

def test_a_mirror_is_reported_as_flipped_rather_than_as_a_half_turn():
    """OSD applies the flip separately, and a flip folded into an angle reads as
    a half turn of a mirrored image -- a different picture."""
    parts = T.decompose((-1, 0, 0, 1, 0, 0))

    assert parts["flipped"] is True
    assert abs(parts["rotation"]) == pytest.approx(0.0)


def test_a_rotation_decomposes_to_its_own_angle():
    angle = math.radians(7)
    parts = T.decompose((math.cos(angle), math.sin(angle), -math.sin(angle), math.cos(angle), 0, 0))

    assert parts["rotation"] == pytest.approx(7.0)
    assert parts["shear"] == pytest.approx(0.0, abs=1e-12)
    assert parts["scale_x"] == pytest.approx(1.0)
