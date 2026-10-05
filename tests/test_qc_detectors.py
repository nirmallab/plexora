"""Detectors and candidates against scenes with known artifacts.

A detector proposes, it never decides -- so what is pinned is recall (every
painted artifact has a candidate overlapping it, of a plausible class),
restraint (a clean scene yields almost nothing), stable ids, and geometry the
ROI plugin accepts.
"""

import numpy as np
import pytest

from plexora.agent import AgentSession
from plexora.plugins.qc.server import candidates as cand, polygons, scan
from plexora.plugins.qc.server.detectors import DetectorContext, run_all
from tests.qc_fixtures import SCAN_PARAMS, make_qc_project


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


def _detect(tmp_path, artifacts, name="qcsynth", seed=0):
    info = make_qc_project(tmp_path, name=name, artifacts=artifacts, seed=seed)
    session = AgentSession()
    result, _ = scan.load_or_run(session, info["name"], params=SCAN_PARAMS)
    raw, skipped = run_all(DetectorContext(result, project=info["name"]))
    built = cand.build(raw, result, project=info["name"])
    return info, result, built, skipped


def _best(built, truth_grid, classes):
    best = (0.0, None)
    for candidate in built["ranked"]:
        inter = np.logical_and(candidate.mask, truth_grid).sum()
        union = np.logical_or(candidate.mask, truth_grid).sum()
        iou = inter / union if union else 0.0
        if candidate.class_hint in classes or set(candidate.alternatives) & set(classes):
            best = max(best, (iou, candidate), key=lambda t: t[0])
    return best


EXPECT = {
    "blur_local": ("out_of_focus",),
    "saturation": ("saturation_or_clipping",),
    "aggregates": ("antibody_aggregate", "debris_or_foreign_object"),
    "fold": ("tissue_fold", "autofluorescence"),
    "dark_region": ("tissue_damage_or_detachment",),
    "cycle_dropout": ("cycle_specific_tissue_loss", "tissue_damage_or_detachment"),
}


@pytest.mark.parametrize("artifact", sorted(EXPECT))
def test_every_painted_artifact_has_a_candidate(tmp_path, artifact):
    info, result, built, _ = _detect(tmp_path, (artifact,))
    region = next(r for r in info["truth"]["regions"] if r["name"] == artifact)
    truth = _truth_on_grid(result, region["mask"])
    iou, found = _best(built, truth, EXPECT[artifact])
    assert found is not None and iou >= 0.3, (
        artifact, iou, [(c.class_hint, c.detector, int(c.mask.sum()), c.channels)
                        for c in built["ranked"]])
    assert set(found.channels) & set(region["channels"])


def test_whole_channel_problems_are_whole_channel_candidates(tmp_path):
    info, result, built, _ = _detect(tmp_path, ("empty_channel", "illumination"))
    classes = {(c.class_hint, c.channels) for c in built["ranked"]}
    assert ("empty_or_failed_channel", ("CD20",)) in classes


def _raw(klass, channels, mask, score=0.9, scope="channel", detector="seam"):
    from plexora.plugins.qc.server.detectors.base import Candidate

    return Candidate(detector=detector, detector_version="1", class_hint=klass,
                     scope_hint=scope, channels=tuple(channels), mask=mask.copy(),
                     score=score, severity=score)


def test_the_same_seam_in_several_channels_is_one_candidate():
    """Merged before anything is shown: one candidate carrying every channel
    (the scope question then says which), not a look per channel."""
    line = np.zeros((20, 20), dtype=bool)
    line[:, 9:11] = True
    blob = np.zeros((20, 20), dtype=bool)
    blob[2:6, 2:6] = True
    raw = [_raw("stitching_or_tile_seam", ["CD3"], line, 1.0),
           _raw("stitching_or_tile_seam", ["CD8"], line, 0.9),
           _raw("stitching_or_tile_seam", ["DNA_1", "CD3", "CD8"], line, 1.0,
                scope="all_channels"),
           _raw("tissue_fold", ["CD3"], line, 0.8, detector="diffuse_bright"),
           _raw("empty_or_failed_channel", ["CD20"], np.ones((20, 20), dtype=bool), 1.0,
                detector="empty"),
           _raw("empty_or_failed_channel", ["CD8"], np.ones((20, 20), dtype=bool), 1.0,
                detector="empty")]
    merged = cand.merge(raw)
    seams = [c for c in merged if c.class_hint == "stitching_or_tile_seam"]
    assert len(seams) == 1
    assert set(seams[0].channels) == {"CD3", "CD8", "DNA_1"}
    assert seams[0].scope_hint == "all_channels"
    # A local class never joins a whole-line one, and failed channels stay apart.
    assert [c.channels for c in merged if c.class_hint == "tissue_fold"] == [("CD3",)]
    assert sorted(c.channels for c in merged if c.class_hint == "empty_or_failed_channel") \
        == [("CD20",), ("CD8",)]


def test_a_clean_scene_yields_almost_nothing(tmp_path):
    info, result, built, _ = _detect(tmp_path, ())
    assert len(built["ranked"]) <= 2, [(c.class_hint, c.detector, c.channels, c.severity)
                                       for c in built["ranked"]]


def test_candidate_ids_survive_a_rerun(tmp_path):
    info, result, built, _ = _detect(tmp_path, ("saturation", "aggregates"))
    raw, _ = run_all(DetectorContext(result, project=info["name"]))
    again = cand.build(raw, result, project=info["name"])
    assert [c.id for c in built["ranked"]] == [c.id for c in again["ranked"]]
    assert all(c.id.startswith("cand_") for c in built["ranked"])


def test_every_candidate_is_geometry_the_roi_plugin_accepts(tmp_path):
    from shapely.geometry import shape

    from plexora.plugins.roi.server.geometry import validate_geometry

    info, result, built, _ = _detect(tmp_path, ("saturation", "fold", "dark_region"))
    assert built["ranked"]
    for candidate in built["ranked"]:
        variants = polygons.geometry_variants(candidate.mask, result.grid, pixel_um=1.0)
        assert {"tight", "standard", "generous", "hull", "bbox"} <= set(variants)
        for variant in variants.values():
            validate_geometry(variant["geometry"])
        tight = shape(variants["tight"]["geometry"])
        standard = shape(variants["standard"]["geometry"])
        assert standard.area >= tight.area
        # The tight outline is the mask's cells, exactly (before simplifying).
        cell = result.grid["cell_full_px"]
        assert tight.area == pytest.approx(candidate.mask.sum() * cell * cell, rel=0.15)
    # ...and so is every trace inside a candidate's standard outline.
    from plexora.plugins.qc.server import refine
    from plexora.server.utils import source_image

    session = AgentSession()
    with source_image.SHELF.reader(session.image_data(info["name"])) as source:
        for candidate in built["ranked"]:
            envelope = polygons._dilate(candidate.mask, 1)
            traced = refine.refine(candidate, envelope, result, source, pixel_um=1.0)
            validate_geometry(traced.geometry)
            assert shape(polygons.mask_to_geometry(envelope, result.grid)).buffer(1e-3) \
                .contains(shape(traced.geometry))


def test_the_grid_fallback_rebuilds_the_squares_named(tmp_path):
    info, result, built, _ = _detect(tmp_path, ("saturation",))
    candidate = built["ranked"][0]
    spec = polygons.localisation_grid(candidate.mask, result.grid, side=8)
    assert spec["squares"] and spec["squares"][0]["id"] == "A1"
    covered = [s["id"] for s in spec["squares"] if s["covered"] > 0.5]
    rebuilt = polygons.rebuild_from_grid(spec, covered, result.grid["shape"])
    inter = np.logical_and(rebuilt, candidate.mask).sum()
    assert inter / max(1, candidate.mask.sum()) > 0.6
