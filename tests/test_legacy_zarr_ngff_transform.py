"""Coordinate transforms read from every place an NGFF version puts them.

NGFF 0.4 keeps `multiscales` at the top of `.zattrs`; 0.5 (Zarr v3, and every
SpatialData store written by spatialdata >= 0.6) nests it under `"ome"` in
`zarr.json`. `ngff_transform` used to read only the first, so each element of a
current SpatialData store came back "aligned by assumption". The shapes below
follow a real spatialdata 0.7.1 store (mibitof): `input`/`output` written as
objects `{name, axes}`, and an `identity` transform.
"""

from __future__ import annotations

import json

import pytest

from plexora.server.utils import ngff_transform, spatial_scene
from tests.spatial_fixtures import (scale, translation, write_element_attrs,
                                    write_spatialdata_store)


def _system(name, axes=("c", "y", "x")):
    return {"name": name, "axes": [{"name": a, "type": "channel" if a == "c" else "space",
                                    **({} if a == "c" else {"unit": "unit"})} for a in axes]}


def _identity(output, axes=("c", "y", "x")):
    # Exactly as spatialdata 0.7.1 writes it.
    return {"type": "identity", "input": _system("".join(axes), axes),
            "output": _system(output, axes)}


@pytest.mark.parametrize("layout, zarr_format", [
    ("flat", 2), ("multiscales", 2), ("multiscales", 3), ("ome", 3)])
def test_scale_and_translation_read_from_every_layout(tmp_path, layout, zarr_format):
    element = write_element_attrs(
        tmp_path / "el", [scale(0.5, 0.5), translation(10.0, 20.0)],
        zarr_format=zarr_format, layout=layout)
    attrs = ngff_transform.read_attrs(element)
    assert ngff_transform.element_transform(attrs, "global") == \
        (0.5, 0.0, 0.0, 0.5, 20.0, 10.0)


@pytest.mark.parametrize("layout, zarr_format", [("multiscales", 2), ("ome", 3)])
def test_a_spatialdata_identity_with_object_systems(tmp_path, layout, zarr_format):
    element = write_element_attrs(tmp_path / "el", [_identity("point16")],
                                  axes=("c", "y", "x"), zarr_format=zarr_format,
                                  layout=layout)
    attrs = ngff_transform.read_attrs(element)
    assert ngff_transform.element_transform(attrs, "point16") == ngff_transform.IDENTITY
    # A system the element does not name is still "not registered".
    assert ngff_transform.element_transform(attrs, "global") is None


def test_string_systems_and_axes_from_the_ome_block(tmp_path):
    # Output named by a bare string, axes only in the nested multiscale: the
    # scale has to be read off x and y, not off the channel axis.
    element = tmp_path / "el"
    element.mkdir()
    (element / "zarr.json").write_text(json.dumps({
        "zarr_format": 3, "node_type": "group", "attributes": {"ome": {
            "version": "0.5", "multiscales": [{
                "axes": [{"name": "c"}, {"name": "y"}, {"name": "x"}],
                "datasets": [{"path": "0"}],
                "coordinateTransformations": [
                    {"type": "scale", "scale": [1.0, 2.0, 3.0], "input": "cyx",
                     "output": "global"}]}]}}}))
    attrs = ngff_transform.read_attrs(element)
    assert ngff_transform.element_transform(attrs, "global") == (3.0, 0.0, 0.0, 2.0, 0.0, 0.0)


def test_a_spatialdata_0_6_store_gets_its_transforms(tmp_path):
    store = write_spatialdata_store(tmp_path / "data.zarr", zarr_format=3, layout="ome",
                                    elements={
                                        "images/he": [scale(0.5, 0.5), translation(4.0, 8.0)],
                                        "labels/cells": [scale(0.5, 0.5)],
                                    })
    found = {e.id: e.to_system for e in spatial_scene.read_spatialdata_scene(store)}
    assert found["images/he"] == (0.5, 0.0, 0.0, 0.5, 8.0, 4.0)
    assert found["labels/cells"] == (0.5, 0.0, 0.0, 0.5, 0.0, 0.0)
