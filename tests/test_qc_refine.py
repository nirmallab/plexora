"""The tracer: an artifact's own pixels inside the envelope the agent judged.

Against scenes with painted artifacts: the trace is valid ROI geometry, lies
inside its envelope, keeps less tissue than the envelope and matches the
truth better, and never loses the artifact -- a trace that misses it falls
back to the envelope. Plus the pieces: mask to polygon, level choice, the
channel budget, determinism.
"""

import numpy as np
import pytest

from plexora.agent import AgentSession
from plexora.plugins.qc.server import candidates as cand, polygons, refine, scan
from plexora.plugins.qc.server.detectors import DetectorContext, run_all
from tests.qc_fixtures import SCAN_PARAMS, make_qc_project

EXPECT = {
    "blur_local": ("out_of_focus",),
    "saturation": ("saturation_or_clipping",),
    "aggregates": ("antibody_aggregate", "debris_or_foreign_object"),
    "fold": ("tissue_fold", "autofluorescence"),
    "dark_region": ("tissue_damage_or_detachment",),
    "cycle_dropout": ("cycle_specific_tissue_loss", "tissue_damage_or_detachment"),
}

#: The share of the painted artifact a trace must cover.
RECALL = {"saturation": 0.98, "dark_region": 0.9, "cycle_dropout": 0.9, "fold": 0.85,
          "blur_local": 0.7}


def rasterise(geometry, size):
    """A GeoJSON polygon as a full-resolution mask of a `size` square image."""
    import cv2
    from shapely.geometry import MultiPolygon, shape

    canvas = np.zeros((size, size), dtype=np.uint8)
    found = shape(geometry)
    for polygon in (found.geoms if isinstance(found, MultiPolygon) else [found]):
        ring = np.asarray(polygon.exterior.coords) - 0.5
        cv2.fillPoly(canvas, [np.round(ring * 16).astype(np.int32).reshape(-1, 1, 2)], 1,
                     shift=4)
        for hole in polygon.interiors:
            ring = np.asarray(hole.coords) - 0.5
            cv2.fillPoly(canvas, [np.round(ring * 16).astype(np.int32).reshape(-1, 1, 2)], 0,
                         shift=4)
    return canvas.astype(bool)


def _iou(a, b):
    union = np.logical_or(a, b).sum()
    return np.logical_and(a, b).sum() / union if union else 0.0


def _truth_on_grid(result, mask):
    grid = result.grid
    ny, nx = grid["shape"]
    s = grid["cell_full_px"]
    out = np.zeros((ny, nx))
    for iy in range(ny):
        for ix in range(nx):
            block = mask[int(iy * s):int((iy + 1) * s), int(ix * s):int((ix + 1) * s)]
            out[iy, ix] = block.mean() if block.size else 0
    return out >= 0.5


def _scene(tmp_path, artifacts, name="qcref", seed=0):
    info = make_qc_project(tmp_path, name=name, artifacts=artifacts, seed=seed)
    session = AgentSession()
    result, _ = scan.load_or_run(session, info["name"], params=SCAN_PARAMS)
    raw, _ = run_all(DetectorContext(result, project=info["name"]))
    built = cand.build(raw, result, project=info["name"])
    return info, session, result, built


def _best(built, result, region, classes):
    truth = _truth_on_grid(result, region["mask"])
    best = (0.0, None)
    for candidate in built["ranked"]:
        if candidate.class_hint in classes or set(candidate.alternatives) & set(classes):
            best = max(best, (_iou(candidate.mask, truth), candidate), key=lambda t: t[0])
    return best[1]


def _envelope(candidate, result):
    """The standard outline (the mask and one cell): the envelope a session
    writes by default."""
    mask = polygons._dilate(candidate.mask, 1)
    return mask, polygons.mask_to_geometry(mask, result.grid)


def _trace(session, info, result, candidate, *, klass=None, options=None, envelope=None):
    from plexora.server.utils import source_image

    if klass is not None:
        candidate.class_hint = klass
    mask, geometry = envelope or _envelope(candidate, result)
    with source_image.SHELF.reader(session.image_data(info["name"])) as source:
        return refine.refine(candidate, mask, result, source, pixel_um=1.0,
                             envelope=geometry, options=options), geometry


@pytest.mark.parametrize("artifact", sorted(EXPECT))
def test_a_trace_keeps_less_than_its_envelope_and_matches_the_truth_better(tmp_path,
                                                                           artifact):
    from shapely.geometry import shape

    from plexora.plugins.roi.server.geometry import validate_geometry

    info, session, result, built = _scene(tmp_path, (artifact,))
    region = next(r for r in info["truth"]["regions"] if r["name"] == artifact)
    candidate = _best(built, result, region, EXPECT[artifact])
    assert candidate is not None
    klass = region["class"]
    traced, envelope = _trace(session, info, result, candidate, klass=klass)
    assert traced.status == "refined", traced.to_record()
    validate_geometry(traced.geometry)
    assert shape(envelope).buffer(1e-6).contains(shape(traced.geometry))
    assert traced.area_px2 <= traced.envelope_area_px2
    size = info["size"]
    truth = region["mask"]
    mine = rasterise(traced.geometry, size)
    theirs = rasterise(envelope, size)
    if artifact == "aggregates":
        # Truth is the disc the specks were thrown into; what matters is that
        # every speck is covered and the tissue between them is kept.
        from scipy import ndimage

        specks = ndimage.binary_dilation(info["image"][info["channels"].index("CD8")]
                                         >= 50_000, iterations=2) & theirs
        assert specks.any()
        assert (mine & specks).sum() >= 0.99 * specks.sum()
        assert mine.sum() <= 0.35 * truth.sum(), (mine.sum(), truth.sum())
        return
    recall = (mine & truth).sum() / truth.sum()
    assert recall >= RECALL[artifact], (artifact, recall, traced.to_record())
    gain = 0.1 if artifact in ("saturation", "fold", "dark_region", "cycle_dropout") else 0.0
    assert _iou(mine, truth) >= _iou(theirs, truth) + gain, (
        artifact, _iou(mine, truth), _iou(theirs, truth))


def test_classes_whose_region_is_the_envelope_read_no_pixels(tmp_path):
    info, session, result, built = _scene(tmp_path, ("saturation",))
    candidate = built["ranked"][0]
    for klass in ("stitching_or_tile_seam", "illumination_or_shading",
                  "empty_or_failed_channel", "uncertain_manual_review"):
        traced, envelope = _trace(session, info, result, candidate, klass=klass)
        assert traced.status == "not_applicable", traced.to_record()
        assert traced.pixels_read == 0
        assert traced.geometry == envelope


def test_nothing_to_trace_falls_back_to_the_envelope(tmp_path):
    """A saturation trace where nothing is saturated or bright (torn,
    dark tissue): the envelope, never an empty region."""
    info, session, result, built = _scene(tmp_path, ("dark_region",))
    candidate = next(c for c in built["ranked"] if c.class_hint in EXPECT["dark_region"])
    traced, envelope = _trace(session, info, result, candidate, klass="saturation_or_clipping")
    assert traced.status == "fallback"
    assert "nothing traced" in traced.reason or "misses" in traced.reason
    assert traced.geometry == envelope and traced.kept_fraction == 1.0


def test_a_trace_that_misses_the_strongest_cells_falls_back(tmp_path, monkeypatch):
    info, session, result, built = _scene(tmp_path, ("saturation",))
    region = next(r for r in info["truth"]["regions"] if r["name"] == "saturation")
    candidate = _best(built, result, region, EXPECT["saturation"])
    envelope_mask, envelope = _envelope(candidate, result)

    def sliver(self):
        # A patch in the envelope's corner, away from the saturated square.
        out = np.zeros(self.crop.shape, dtype=bool)
        ys, xs = np.nonzero(self.envelope_px)
        out[ys.min():ys.min() + 12, xs.min():xs.min() + 12] = True
        return out

    monkeypatch.setattr(refine._Evidence, "saturation", sliver)
    traced, _ = _trace(session, info, result, candidate, envelope=(envelope_mask, envelope))
    assert traced.status == "fallback", traced.to_record()
    assert "strongest cell" in traced.reason
    assert traced.geometry == envelope


def test_the_same_envelope_traces_to_the_same_outline(tmp_path):
    info, session, result, built = _scene(tmp_path, ("fold",))
    region = next(r for r in info["truth"]["regions"] if r["name"] == "fold")
    candidate = _best(built, result, region, EXPECT["fold"])
    first, _ = _trace(session, info, result, candidate, klass="tissue_fold")
    second, _ = _trace(session, info, result, candidate, klass="tissue_fold")
    assert first.status == "refined"
    assert polygons.geometry_hash(first.geometry) == polygons.geometry_hash(second.geometry)
    wider, _ = _trace(session, info, result, candidate, klass="tissue_fold",
                      options={"margin_um": 20})
    assert wider.area_px2 > first.area_px2
    assert wider.params["margin_um"] == 20


def test_a_fold_reads_the_nuclear_stain_first_and_at_most_four_channels(tmp_path):
    info, session, result, built = _scene(tmp_path, ("fold",))
    region = next(r for r in info["truth"]["regions"] if r["name"] == "fold")
    candidate = _best(built, result, region, EXPECT["fold"])
    traced, _ = _trace(session, info, result, candidate, klass="tissue_fold")
    assert traced.channels[0] == "DNA_1"
    assert len(traced.channels) <= refine.REFINE["max_channels"]
    assert traced.pixels_read <= refine.REFINE["max_total_pixels"]
    record = traced.to_record()
    assert "mask" not in record and "geometry" not in record
    import json

    json.dumps(record)


def test_mask_to_polygon_keeps_holes_and_islands():
    from shapely.geometry import shape

    yy, xx = np.mgrid[0:200, 0:200]
    r = np.hypot(yy - 100, xx - 100)
    mask = (r <= 80) & (r > 40)
    mask |= r <= 15
    geometry = refine.mask_to_polygon(mask, (10, 20), 2.0, simplify_px=0)
    found = shape(geometry)
    assert geometry["type"] == "MultiPolygon" and len(found.geoms) == 2
    assert sorted(len(p.interiors) for p in found.geoms) == [0, 1]
    back = rasterise(geometry, 500)[40:440, 20:420]
    expected = np.kron(mask, np.ones((2, 2), dtype=bool))
    assert _iou(back, expected) >= 0.98
    # One pixel and a one-pixel line are outlines too.
    tiny = np.zeros((20, 20), dtype=bool)
    tiny[3, 3] = True
    tiny[10, 2:15] = True
    assert len(shape(refine.mask_to_polygon(tiny, (0, 0), 1.0, simplify_px=0)).geoms) == 2


def test_many_specks_merge_before_any_is_dropped():
    class Crop:
        um = 1.0

    mask = np.zeros((400, 400), dtype=bool)
    mask[2::20, 2::20] = True
    mask = refine._dilate(mask, 2)
    envelope = np.ones_like(mask)
    assert refine._components(mask)[0] - 1 == 400
    capped, parts, dropped = refine._cap_parts(mask, Crop(), envelope)
    assert parts <= refine.REFINE["max_parts"] and dropped == 0
    assert (capped | ~mask).all(), "merging never uncovers a speck"


class _FakeSource:
    def __init__(self, width, levels):
        self.levels = levels
        self._shapes = [(width // 2 ** k, width // 2 ** k) for k in range(levels)]

    def level_shape(self, index):
        return self._shapes[index]


@pytest.mark.parametrize("pixel_um, level", [(0.325, 2), (0.65, 1), (1.0, 0)])
def test_the_level_is_about_a_micron_a_pixel(pixel_um, level):
    chosen, why = refine.choose_level(_FakeSource(40_000, 6), (10_000, 10_000, 10_400, 10_400),
                                      pixel_um, 1)
    assert why is None and chosen[0] == level


def test_a_large_envelope_is_read_coarser_and_a_huge_one_not_at_all():
    source = _FakeSource(100_000, 6)
    chosen, _ = refine.choose_level(source, (0, 0, 8_000, 8_000), 0.65, 4)
    assert chosen[0] >= 2 and chosen[2] <= refine.REFINE["max_um"]
    x0, y0, x1, y1 = chosen[3]
    assert (x1 - x0) * (y1 - y0) <= refine.REFINE["max_total_pixels"] / 4
    chosen, why = refine.choose_level(_FakeSource(100_000, 3), (0, 0, 90_000, 90_000), 0.65, 1)
    assert chosen is None and why
