"""A SpatialData `sequence` transform is composed, not read as the identity.

spatialdata writes a composed transform -- `Sequence([Scale, Translation])`,
what `set_transformation` stores after an alignment or a crop -- as ONE entry
`{"type": "sequence", "transformations": [...], "output": {"name": "global"}}`.
`ngff_transform.transform_list` knew scale, translation and affine only, skipped
the sequence entry and returned the identity it started from, so the element
came back as sitting exactly on the reference: drawn at the wrong scale and
offset while claiming to be registered. The module's own rule is "absent is not
identity": a transform it cannot evaluate must come back as None, so the viewer
says "aligned by assumption" instead.
"""

from __future__ import annotations

import numpy as np
import pytest

from plexora.server.utils import ngff_transform, spatial_scene

spatialdata = pytest.importorskip("spatialdata")


def _image(transform):
    from spatialdata.models import Image2DModel

    pixels = np.random.default_rng(0).integers(1, 100, (1, 32, 32)).astype("uint16")
    return Image2DModel.parse(pixels, dims=("c", "y", "x"),
                              transformations={"global": transform})


def _store_with_a_sequence(path):
    from spatialdata.transformations import Identity, Scale, Sequence, Translation

    spatialdata.SpatialData(images={
        "a_reference": _image(Identity()),
        "b_moved": _image(Sequence([Scale([2.0, 2.0], axes=("y", "x")),
                                    Translation([100.0, 50.0], axes=("y", "x"))])),
    }).write(path)
    return path


def test_a_sequence_transform_written_by_spatialdata_is_composed(tmp_path):
    """Scale by 2, then move 50 right and 100 down: pixel (0, 0) lands at
    (50, 100) and pixel (1, 1) at (52, 102) -- not where it started."""
    store = _store_with_a_sequence(tmp_path / "scene.zarr")

    found = {e.id: e.to_system for e in spatial_scene.read_spatialdata_scene(store)}

    assert found["images/b_moved"] == pytest.approx((2.0, 0.0, 0.0, 2.0, 50.0, 100.0))


def test_a_layer_with_a_sequence_transform_is_placed_where_the_store_says(tmp_path):
    """The registered layer, in the reference image's pixel grid."""
    store = _store_with_a_sequence(tmp_path / "scene.zarr")

    layers = spatial_scene.layers_for(spatial_scene.read_spatialdata_scene(store))
    moved = next(layer for layer in layers if layer.id == "images/b_moved")

    assert moved.transform is not None
    assert tuple(moved.transform) == pytest.approx((2.0, 0.0, 0.0, 2.0, 50.0, 100.0))


def test_a_transform_type_that_cannot_be_evaluated_is_not_read_as_identity():
    """A non-linear NGFF transform (a displacement field) cannot be flattened
    to six numbers. The element is unregistered, not registered at the origin."""
    attrs = {
        "axes": [{"name": "y"}, {"name": "x"}],
        "coordinateTransformations": [
            {"type": "displacements", "path": "field", "output": {"name": "global"}},
        ],
    }

    assert ngff_transform.element_transform(attrs, "global") is None
