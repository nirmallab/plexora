"""A GEOS refusal is asked again, never the end of a run.

The website's GEOS (3.12.1, WebAssembly) now and then refuses a valid outline
-- "CGAlgorithmsDD::orientationIndex encountered NaN/Inf numbers", in a
topology-preserving simplify -- and not reliably for the same input: about one
outline in 300 of the artifact detector's, a different one each run (measured
in the website's own Pyodide; the desktop's GEOS 3.13 never raised). It failed
the whole artifact run. `mask_polygon.geos_again` asks a second way; the
detector drops the one outline GEOS still refuses. Played here by a simplify
that refuses the first time it is asked.
"""

import numpy as np
import pytest
import shapely
from shapely.geometry import box

from plexora.server.utils import mask_polygon


def _refusing(monkeypatch, times=1):
    real = shapely.simplify
    calls = {"refused": 0}

    def simplify(geometry, tolerance, preserve_topology=True, **kwargs):
        if preserve_topology and calls["refused"] < times:
            calls["refused"] += 1
            raise shapely.errors.GEOSException(
                "IllegalArgumentException: CGAlgorithmsDD::orientationIndex encountered NaN/Inf numbers")
        return real(geometry, tolerance, preserve_topology=preserve_topology, **kwargs)

    monkeypatch.setattr(shapely, "simplify", simplify)
    return calls


def _shape():
    return box(10, 10, 60, 40).union(box(30, 20, 90, 70))


def test_a_refused_simplify_is_asked_again_and_answers_the_same(monkeypatch):
    plain = mask_polygon.to_geojson(_shape(), simplify_px=0.7)
    calls = _refusing(monkeypatch)
    again = mask_polygon.to_geojson(_shape(), simplify_px=0.7)
    assert calls["refused"] == 1
    # The same outline (the grid it is snapped to starts its ring elsewhere).
    assert shapely.geometry.shape(again).equals(shapely.geometry.shape(plain))


def test_refused_twice_the_outline_is_simplified_without_topology(monkeypatch):
    _refusing(monkeypatch, times=2)
    geometry = mask_polygon.to_geojson(_shape(), simplify_px=0.7)
    assert geometry is not None and geometry["type"] == "Polygon"
    made = shapely.geometry.shape(geometry)
    assert made.is_valid and made.area == pytest.approx(_shape().area, rel=0.02)


def test_geos_again_raises_the_last_refusal():
    def refuse():
        raise shapely.errors.GEOSException("no")

    assert mask_polygon.geos_again(refuse, lambda: 7) == 7
    with pytest.raises(shapely.errors.GEOSException):
        mask_polygon.geos_again(refuse, refuse)


def test_the_artifact_detector_drops_one_outline_rather_than_the_run(monkeypatch):
    from plexora.plugins.qc.server import artifacts, polygons

    def refuse(*args, **kwargs):
        raise shapely.errors.GEOSException("orientationIndex encountered NaN/Inf numbers")

    monkeypatch.setattr(polygons, "to_geojson", refuse)
    assert artifacts._geojson(_shape(), 1.0, 500) is None


def test_the_detector_s_outline_keeps_a_one_pixel_fiber():
    from plexora.plugins.qc.server import artifacts

    mask = np.zeros((20, 30), bool)
    mask[5:15, 4:12] = True
    mask[9, 12:28] = True          # a fiber one pixel wide
    shape = artifacts._shape_of(mask, (1000, 2000), 4.0)
    ys, xs = np.nonzero(mask)
    centres = shapely.points((xs + 0.5 + 1000) * 4.0, (ys + 0.5 + 2000) * 4.0)
    assert shapely.covers(shape, centres).all()
    assert np.isfinite(shapely.get_coordinates(shape)).all()
