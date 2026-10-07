"""The visual pass outside a session: the overview sheet with a frame per
tile, the channel ranking and sheet, and `segment_qc_roi`'s guards and
provenance -- a point on a sheet, a retry from the last mask, the tissue
cut, a duplicate refused, `unsure` in Needs review, the cells failing, the
report saying who drew it -- all through `FakeSamBackend`."""

import json
import os
from pathlib import Path

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.plugins.qc.server import schemas
from plexora.vision import sam, sam_backend
from plexora.vision import segment as segmenter
from tests.qc_fixtures import make_qc_project

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])
    segmenter.forget()
    yield
    segmenter.forget()


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


def _centre(info, name):
    region = next(r for r in info["truth"]["regions"] if r["name"] == name)
    ys, xs = np.nonzero(region["mask"])
    return float(xs.mean()), float(ys.mean())


def _dump(name, result):
    """With PLEXORA_VISUAL_DUMP naming a folder, keep the picture a look
    returned, for a person to look at."""
    folder = os.environ.get("PLEXORA_VISUAL_DUMP")
    if folder and result.get("_images"):
        png = result["_images"][0]
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / f"{name}.png").write_bytes(png if isinstance(png, bytes)
                                                   else png["data"])


def _on_tile(frame, x, y):
    """A full-resolution point as the sheet pixels of `frame`."""
    b = frame["bounds_fullres"]
    return [frame["origin"][0] + (x - b["x"]) * frame["size"][0] / b["width"],
            frame["origin"][1] + (y - b["y"]) * frame["size"][1] / b["height"]]


# -- pure helpers ------------------------------------------------------------------------


def test_a_pixel_maps_through_the_tile_under_it_and_a_gutter_is_refused():
    from plexora.agent.errors import AgentError
    from plexora.plugins.qc.server import frames

    bounds = {"x": 100, "y": 200, "width": 400, "height": 400}
    manifest = {"frames": [
        {"slot": 0, "panel": "a", "origin": [4, 30], "size": [100, 100], "bounds_fullres": bounds},
        {"slot": 1, "panel": "b", "origin": [108, 30], "size": [100, 100],
         "bounds_fullres": bounds}]}
    frame = frames.frame_at(manifest, (158, 80))
    assert frame["panel"] == "b"
    assert frames.to_image(frame, (158, 80)) == (300.0, 400.0)
    with pytest.raises(AgentError) as refused:
        frames.frame_at(manifest, (106, 80))
    assert "tile" in str(refused.value)
    single = {"bounds_fullres": {"x": 0, "y": 0, "width": 512, "height": 512},
              "output_size": [256, 256]}
    assert frames.to_image(frames.frame_at(single, (128, 64)), (128, 64)) == (256.0, 128.0)
    assert frames.frames_of({}) == []
    assert frames.picture_channels({"channels": {"dna": ["DNA_1"]}}) is None


def test_overlap_of_two_regions():
    from plexora.plugins.qc.server import polygons

    a, b = _box(0, 0, 100, 100), _box(50, 0, 150, 100)
    found = polygons.overlap(a, b)
    assert found["iou"] == pytest.approx(1 / 3) and found["share_a_in_b"] == pytest.approx(0.5)
    assert polygons.overlap(a, _box(0, 0, 100, 100))["iou"] == pytest.approx(1.0)
    assert polygons.overlap(a, _box(200, 200, 300, 300))["iou"] == 0.0
    assert polygons.overlap(a, None)["iou"] == 0.0


def test_an_outline_is_cut_to_the_tissue_and_one_on_the_glass_is_refused():
    from plexora.plugins.qc.server import polygons, tissue

    mask = np.zeros((64, 64), dtype=bool)
    mask[16:48, 16:48] = True          # at factor 8: the tissue is 128..384 px
    found = {"mask": mask, "feathered": mask, "factor": 8.0, "pixel_um": None,
             "method": "otsu"}
    inside = _box(200, 200, 300, 300)
    assert tissue.share_on_tissue(found, inside)["on_tissue_fraction"] == pytest.approx(1.0)
    assert tissue.clip_to_tissue(found, inside) is not None
    glass = _box(0, 0, 60, 60)
    assert tissue.share_on_tissue(found, glass)["on_tissue_fraction"] == 0.0
    assert tissue.clip_to_tissue(found, glass) is None
    straddling = _box(0, 200, 300, 300)
    share = tissue.share_on_tissue(found, straddling)["on_tissue_fraction"]
    assert 0.3 < share < 0.9
    cut = tissue.clip_to_tissue(found, straddling)
    assert polygons.area_of(cut) < polygons.area_of(straddling)
    assert tissue.share_on_tissue(found, cut)["on_tissue_fraction"] == pytest.approx(1.0, abs=0.05)


# -- the looks -----------------------------------------------------------------------------


def test_the_overview_is_cropped_to_the_tissue_with_a_frame_per_tile(tmp_path):
    make_qc_project(tmp_path, size=512, grid=20, artifacts=("fold",))
    session = AgentSession()
    first = ok(invoke(session, "render_artifact_overview", {"project": "qcsynth"}))
    _dump("overview", first)
    manifest = first["manifest"]
    frames = manifest["frames"]
    assert [f["panel"] for f in frames] == ["dna_max", "dna_cycles", "pan", "agreement"]
    assert manifest["tissue"]["cropped_to_tissue"] is True
    bounds = frames[0]["bounds_fullres"]
    assert bounds["x"] > 0 and bounds["x"] + bounds["width"] < 512
    assert all(f["bounds_fullres"] == bounds for f in frames)
    assert first.get("image_inline") is True
    assert manifest["grid"]["rows"] == schemas.VISUAL["grid_side"]
    assert manifest["channels"]["dna"] == ["DNA_1", "DNA_2"]
    again = ok(invoke(session, "render_artifact_overview", {"project": "qcsynth"}))
    assert again["artifact"]["id"] == first["artifact"]["id"]
    f = frames[0]
    px = [f["origin"][0] + f["size"][0] * 0.6, f["origin"][1] + f["size"][1] * 0.4,
          f["origin"][0] + f["size"][0] * 0.9, f["origin"][1] + f["size"][1] * 0.8]
    zoomed = ok(invoke(session, "render_artifact_overview", {
        "project": "qcsynth", "region": {"artifact_id": first["artifact"]["id"], "px": px},
        "panels": ["dna_max", "pan"]}))
    _dump("overview_zoom", zoomed)
    zb = zoomed["manifest"]["frames"][0]["bounds_fullres"]
    assert zoomed["manifest"]["zoomed"] and zb["width"] < bounds["width"]
    assert [f["panel"] for f in zoomed["manifest"]["frames"]] == ["dna_max", "pan"]
    refused = invoke(session, "render_artifact_overview", {
        "project": "qcsynth", "region": {"artifact_id": first["artifact"]["id"],
                                         "px": [1, 1, 5, 5]}})
    assert not refused["ok"] and "tile" in refused["error"]["message"]


def test_the_channel_sheet_ranks_the_channel_that_shows_the_artifact_first(tmp_path):
    info = make_qc_project(tmp_path, size=512, grid=20, artifacts=("aggregates",))
    cx, cy = _centre(info, "aggregates")
    session = AgentSession()
    found = ok(invoke(session, "inspect_artifact_channels", {
        "project": "qcsynth", "box": {"x": cx - 40, "y": cy - 40, "width": 80, "height": 80}}))
    _dump("channels", found)
    assert found["ranked"][0]["channel"] == "CD8"
    assert found["ranked"][0]["direction"] == "brighter"
    assert "CD8" in found["shown"] and len(found["frames"]) == 1 + len(found["shown"])
    assert found["ring_on_tissue_fraction"] > 0.5
    assert found.get("image_inline") is True
    sheet = ok(invoke(session, "render_artifact_overview", {"project": "qcsynth"}))
    tile = sheet["manifest"]["frames"][2]
    px = [*_on_tile(tile, cx - 40, cy - 40), *_on_tile(tile, cx + 40, cy + 40)]
    same = ok(invoke(session, "inspect_artifact_channels", {
        "project": "qcsynth", "box": {"artifact_id": sheet["artifact"]["id"], "px": px},
        "top": 3}))
    assert same["ranked"][0]["channel"] == "CD8" and len(same["shown"]) == 3


def test_an_image_of_many_channels_keeps_its_dna_and_autofluorescence_in_reach(
        tmp_path, monkeypatch):
    """A hundred and twenty channels: the answer stays a shortlist, yet every
    DNA and autofluorescence channel is in it wherever it ranks, a channel
    named in `channels` is on the sheet, and only the sheet's planes are held."""
    import tracemalloc

    from tests import qc_fixtures

    scene = qc_fixtures.qc_scene

    def wide(*args, **kwargs):
        image, labels, cells, channels, truth = scene(*args, **kwargs)
        rng = np.random.default_rng(7)
        filler = rng.normal(400, 20, (100, *image.shape[1:])).clip(0).astype(image.dtype)
        late_af = rng.normal(300, 10, (1, *image.shape[1:])).clip(0).astype(image.dtype)
        names = [*channels, *[f"M{i:03d}" for i in range(100)], "AF9"]
        return np.concatenate([image, filler, late_af]), labels, cells, names, truth

    monkeypatch.setattr(qc_fixtures, "qc_scene", wide)
    info = make_qc_project(tmp_path, size=512, grid=20, artifacts=("aggregates",))
    cx, cy = _centre(info, "aggregates")
    box = {"x": cx - 40, "y": cy - 40, "width": 80, "height": 80}
    session = AgentSession()
    found = ok(invoke(session, "inspect_artifact_channels", {"project": "qcsynth", "box": box}))
    names = [r["channel"] for r in found["ranked"]]
    assert names[0] == "CD8" and found["ranked_more"] >= 90
    assert "AF9" in names and next(r for r in found["ranked"]
                                   if r["channel"] == "AF9")["autofluorescence"]
    assert all(r["channel"] in names for r in found["ranked"] if r["nuclear"])
    asked = ok(invoke(session, "inspect_artifact_channels", {
        "project": "qcsynth", "box": box, "channels": ["M050"], "top": 4}))
    assert asked["shown"][0] == "M050" and asked["shown"][1] == "CD8"
    assert len(asked["shown"]) == 4
    refused = invoke(session, "inspect_artifact_channels", {
        "project": "qcsynth", "box": box, "channels": ["nope"]})
    assert not refused["ok"] and "nope" in refused["error"]["message"]
    # Held planes: the sheet's, not every channel's.
    from plexora.plugins.qc.server import overview

    tracemalloc.start()
    ranking = overview.rank_channels(session, "qcsynth", box, top=4)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    plane = next(iter(ranking["planes"].values())).nbytes
    # The reader's own decoding peaks at some fifty planes' worth whatever the
    # channel count; holding every channel would be ~250 (122 float64 planes).
    assert len(ranking["planes"]) == 4 and peak < 80 * plane


# -- the write -----------------------------------------------------------------------------


@pytest.fixture
def specks(tmp_path):
    info = make_qc_project(tmp_path, size=512, grid=20, artifacts=("speck_tissue",))
    info["speck"] = _centre(info, "speck_tissue")
    return info


def test_a_preview_shows_two_panels_and_a_retry_reuses_the_embedding(specks):
    session = AgentSession()
    cx, cy = specks["speck"]
    backend = sam_backend.FakeSamBackend()
    args = {"project": "qcsynth", "artifact_class": "debris_or_foreign_object", "preview": True}
    with sam.use_backend(backend):
        first = ok(invoke(session, "segment_qc_roi", {
            **args, "points": [{"x": cx, "y": cy, "label": 1}]}))
        _dump("preview", first)
        assert first["preview"] and [f["panel"] for f in first["frames"]] == ["fit", "context"]
        assert first["on_tissue_fraction"] == pytest.approx(1.0) and first["overlaps"] == []
        assert first["sam"]["token"] and first["sam"]["prev_key"]
        again = ok(invoke(session, "segment_qc_roi", {
            **args, "points": [{"x": cx, "y": cy, "label": 1}, {"x": cx + 20, "y": cy, "label": 0}],
            "continue_from": first["sam"]}))
        assert again["restarted"] is False
    assert sum(1 for c in backend.calls if c[0] == "embed") == 1
    assert sum(1 for c in backend.calls if c[0] == "decode") == 2
    assert ok(invoke(session, "list_rois", {"project": "qcsynth"}))["count"] == 0
    context = next(f for f in first["frames"] if f["panel"] == "context")
    with sam.use_backend(sam_backend.FakeSamBackend()):
        third = ok(invoke(session, "segment_qc_roi", {
            **args, "points": [{"artifact_id": first["artifact"]["id"],
                                "px": _on_tile(context, cx, cy)}]}))
    x0, y0, x1, y1 = third["outline"]["bounds"]
    assert x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2
    assert "geometry" not in third and third["outline"]["vertices"] >= 4


def test_a_point_on_the_overview_outlines_the_object_under_it(specks):
    session = AgentSession()
    cx, cy = specks["speck"]
    sheet = ok(invoke(session, "render_artifact_overview", {"project": "qcsynth"}))
    tile = sheet["manifest"]["frames"][1]
    with sam.use_backend(sam_backend.FakeSamBackend()):
        made = ok(invoke(session, "segment_qc_roi", {
            "project": "qcsynth",
            "points": [{"artifact_id": sheet["artifact"]["id"], "px": _on_tile(tile, cx, cy)}],
            "artifact_class": "debris_or_foreign_object",
            "evidence_artifacts": [sheet["artifact"]["id"]]}))
    assert made["written"]
    left, top, right, bottom = ok(invoke(session, "get_roi", {
        "project": "qcsynth", "roi_id": made["roi_id"]}))["roi"]["bounds"]
    assert left <= cx <= right and top <= cy <= bottom and (right - left) < 60


def test_a_region_written_carries_the_agents_reasoning_and_its_cells_fail(specks):
    from plexora.plugins.qc.server import results

    session = AgentSession()
    cell = specks["cells"][len(specks["cells"]) // 2]
    cx, cy = float(cell["x"]), float(cell["y"])
    with sam.use_backend(sam_backend.FakeSamBackend()):
        sheet = ok(invoke(session, "render_artifact_overview", {"project": "qcsynth"}))
        made = ok(invoke(session, "segment_qc_roi", {
            "project": "qcsynth",
            "box": {"x": cx - 30, "y": cy - 30, "width": 60, "height": 60},
            "artifact_class": "tissue_fold", "confidence": "sure", "severity": "severe",
            "reasoning": "A bright doubled band in the DNA and the pan view.",
            "evidence_artifacts": [sheet["artifact"]["id"]]}))
    assert made["written"] and made["action"] == "exclude", made
    assert made["measurement"]["support"] == "traced"
    stored = results.active(results.load("qcsynth"))["candidates"][made["candidate_id"]]
    assert stored["ai_decision"]["reasoning"].startswith("A bright")
    assert stored["notes"][0].startswith("A bright")
    assert stored["evidence_artifacts"] == [sheet["artifact"]["id"]]
    assert stored["state"] == "confirmed_exclude" and stored["origin"] == "agent"
    found = ok(invoke(session, "get_qc_results", {"project": "qcsynth", "include_cells": True}))
    assert cell["id"] in found["failing_cell_ids"]
    mine = next(r for r in found["regions"] if r["roi_id"] == made["roi_id"])
    assert mine["origin"] == "agent" and mine["method"] == "sam_agent"
    html = Path(ok(invoke(session, "qc_report", {"project": "qcsynth", "format": "html"}))
                ["html"]).read_text(encoding="utf-8")
    assert "magic select" in html and "A bright doubled band" in html


def test_a_duplicate_is_refused_and_force_writes_it(specks):
    session = AgentSession()
    cx, cy = specks["speck"]
    args = {"project": "qcsynth", "points": [{"x": cx, "y": cy, "label": 1}],
            "artifact_class": "debris_or_foreign_object"}
    with sam.use_backend(sam_backend.FakeSamBackend()):
        first = ok(invoke(session, "segment_qc_roi", args))
        second = ok(invoke(session, "segment_qc_roi", args))
        assert first["written"] and not second["written"]
        assert second["duplicate_of"]["roi_id"] == first["roi_id"]
        assert "already covers" in second["why"]
        preview = ok(invoke(session, "segment_qc_roi", {**args, "preview": True}))
        assert preview["duplicate_of"]["roi_id"] == first["roi_id"]
        forced = ok(invoke(session, "segment_qc_roi", {**args, "force_duplicate": True}))
        assert forced["written"] and forced["roi_id"] != first["roi_id"]
        assert forced["candidate_id"] != first["candidate_id"]
    assert ok(invoke(session, "list_rois", {"project": "qcsynth"}))["count"] == 2


def test_a_point_on_a_coarse_picture_asks_for_a_view_at_its_scale():
    """A click on a whole-slide sheet (tens of image pixels to a sheet pixel)
    is a place millimetres across, not a 256 px crop."""
    from plexora.plugins.qc import capabilities_segment as seg
    from plexora.plugins.qc.server import frames

    frame = {"origin": [0, 0], "size": [400, 500],
             "bounds_fullres": {"x": 0, "y": 0, "width": 14000, "height": 17500}}
    scale = frames.scale_of(frame)
    assert scale == pytest.approx(35.0)
    point = [{"x": 7000.0, "y": 9000.0, "label": 1}]
    near = seg._view(point, None, None, (45000, 52000))
    far = seg._view(point, None, None, (45000, 52000), seg.POINT_PICTURE_PX * scale)
    assert near["width"] == pytest.approx(seg.MIN_VIEW_PX)
    assert far["width"] == pytest.approx(seg.POINT_PICTURE_PX * scale)


def test_box_only_previews_keep_their_own_masks(specks):
    """Two box-only previews have different keys, and a retry may name the
    earlier one: its mask is still there."""
    session = AgentSession()
    cx, cy = specks["speck"]
    args = {"project": "qcsynth", "artifact_class": "debris_or_foreign_object", "preview": True}
    with sam.use_backend(sam_backend.FakeSamBackend()):
        first = ok(invoke(session, "segment_qc_roi", {
            **args, "box": {"x": cx - 14, "y": cy - 14, "width": 28, "height": 28}}))
        second = ok(invoke(session, "segment_qc_roi", {
            **args, "box": {"x": cx - 12, "y": cy - 12, "width": 24, "height": 24}}))
        assert first["sam"]["token"] == second["sam"]["token"]
        assert first["sam"]["prev_key"] != second["sam"]["prev_key"]
        again = ok(invoke(session, "segment_qc_roi", {
            **args, "box": {"x": cx - 14, "y": cy - 14, "width": 28, "height": 28},
            "continue_from": first["sam"]}))
        assert again["restarted"] is False
        gone = ok(invoke(session, "segment_qc_roi", {
            **args, "box": {"x": cx - 14, "y": cy - 14, "width": 28, "height": 28},
            "continue_from": {"token": first["sam"]["token"], "prev_key": "000000000000"}}))
        assert gone["restarted"] is True


def test_a_check_region_on_the_glass_is_no_finding(specks):
    from types import SimpleNamespace

    from plexora.plugins.qc.server import check_candidates

    session = AgentSession()
    cx, cy = specks["speck"]
    engine = SimpleNamespace(call=SimpleNamespace(session=session))
    glass = {"id": "g", "geometry": _box(2, 200, 14, 300)}
    tissue = {"id": "t", "geometry": _box(cx - 10, cy - 10, cx + 10, cy + 10)}
    kept, dropped = check_candidates.on_tissue(engine, "qcsynth", [glass, tissue])
    assert [r["id"] for r in kept] == ["t"] and dropped == 1


def test_a_region_the_outline_swallows_is_an_overlap(specks):
    """A small region inside a larger outline overlaps it, though the outline
    lies almost wholly outside the region."""
    session = AgentSession()
    cx, cy = specks["speck"]
    with sam.use_backend(sam_backend.FakeSamBackend()):
        small = ok(invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "points": [{"x": cx, "y": cy, "label": 1}],
            "artifact_class": "debris_or_foreign_object"}))
        wide = ok(invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "box": {"x": cx - 60, "y": cy - 60, "width": 120, "height": 120},
            "artifact_class": "tissue_fold", "preview": True}))
    row = next(r for r in wide["overlaps"] if r["roi_id"] == small["roi_id"])
    assert row["covers_share"] > 0.5 and row["inside_share"] < schemas.ENGINE["merge_contain"]


def test_an_outline_on_the_glass_is_refused(specks):
    session = AgentSession()
    with sam.use_backend(sam_backend.FakeSamBackend()):
        refused = ok(invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "box": {"x": 2, "y": 200, "width": 12, "height": 100},
            "artifact_class": "debris_or_foreign_object"}))
    assert refused["written"] is False and "glass" in refused["why"]
    assert refused["on_tissue_fraction"] < schemas.VISUAL["min_on_tissue"]
    assert ok(invoke(session, "list_rois", {"project": "qcsynth"}))["count"] == 0


def test_unsure_lands_in_needs_review_and_never_excludes(specks):
    session = AgentSession()
    cx, cy = specks["speck"]
    with sam.use_backend(sam_backend.FakeSamBackend()):
        made = ok(invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "points": [{"x": cx, "y": cy, "label": 1}],
            "artifact_class": "debris_or_foreign_object", "confidence": "unsure",
            "severity": "severe"}))
    assert made["class"] == "uncertain_manual_review" and made["action"] == "warn"
    roi = ok(invoke(session, "get_roi", {"project": "qcsynth", "roi_id": made["roi_id"]}))["roi"]
    assert roi["category_id"] == "qc_review"
    ok(invoke(session, "set_qc_strictness", {"project": "qcsynth", "preset": "strict"}))
    regions = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))["regions"]
    mine = next(r for r in regions if r["roi_id"] == made["roi_id"])
    assert mine["action"] == "warn" and mine["class"] == "uncertain_manual_review"


def test_bad_evidence_and_a_session_that_is_not_in_its_pass_are_refused(specks):
    session = AgentSession()
    cx, cy = specks["speck"]
    with sam.use_backend(sam_backend.FakeSamBackend()):
        stranger = invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "points": [{"x": cx, "y": cy, "label": 1}],
            "artifact_class": "debris_or_foreign_object",
            "evidence_artifacts": ["art_0000000000000000"]})
        assert not stranger["ok"] and "artifact" in stranger["error"]["message"]
        reshaping = invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "points": [{"x": cx, "y": cy, "label": 1}],
            "roi_id": "roi_none", "mode": "replace", "session_id": "qs_none"})
        assert not reshaping["ok"] and "mode new" in reshaping["error"]["message"]
