"""QC's smaller promises: AnnData writes, overlap membership, the mirror
script, sheet determinism, strictness bands, cycle inference."""

import dataclasses
import json

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.agent.errors import AgentError
from plexora.agent.policy import Policy
from tests.qc_fixtures import make_qc_project


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


# -- AnnData --------------------------------------------------------------------------------


def _anndata_project(tmp_path, info):
    import anndata as ad
    import pandas as pd

    from tests.helpers import ALL_CONFIRMED, anndata_spec, image_spec, project

    cells = info["cells"]
    markers = list(info["channels"])
    X = np.array([[c[m] for m in markers] for c in cells], dtype=np.float32)
    adata = ad.AnnData(X=X, obs=pd.DataFrame({"imageid": ["A"] * len(cells)},
                                             index=[f"cell_{c['id']}" for c in cells]),
                       var=pd.DataFrame(index=markers))
    adata.obsm["spatial"] = np.array([[c["x"], c["y"]] for c in cells], dtype=np.float64)
    adata.uns["theirs"] = "left alone"
    path = tmp_path / "cells.h5ad"
    adata.write_h5ad(path)
    record = project("qcad", dataset=anndata_spec(path, markers=markers),
                     image=image_spec(channels=markers, width=info["size"],
                                      height=info["size"], src=info["image_path"]),
                     confirmed=ALL_CONFIRMED)
    record = dataclasses.replace(record, image=dataclasses.replace(
        record.image, max_level=3, tile_width=128, tile_height=128))
    config_path = tmp_path / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config["qcad"] = record.to_entry()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_the_calls_are_written_into_an_anndata_file(tmp_path):
    import anndata as ad

    from plexora.plugins.qc.server import schemas

    info = make_qc_project(tmp_path, size=512, grid=20, table=False, mask=False)
    path = _anndata_project(tmp_path, info)
    session = AgentSession()
    ok(invoke(session, "create_roi", {"project": "qcad", "category": "QC: Tissue fold",
                                      "geometry": _box(0, 0, 256, 256)}))
    ok(invoke(session, "refresh_qc", {"project": "qcad"}))
    written = ok(invoke(session, "write_qc_to_source", {"project": "qcad", "confirm": True},
                        policy=Policy.from_flags(allow_source_writes=True)))
    assert written["written"]["n_fail"] > 0
    adata = ad.read_h5ad(path)
    assert str(adata.obs["plexora_qc_pass"].dtype) == "boolean"
    inside = [f"cell_{c['id']}" for c in info["cells"] if c["x"] < 256 and c["y"] < 256]
    assert not adata.obs.loc[inside, "plexora_qc_pass"].any()
    assert (adata.obs.loc[inside, "plexora_qc_primary_reason"] == "region:tissue_fold").all()
    assert (adata.obs.loc[inside, "plexora_qc_category"] == "tissue_acquisition").all()
    assert json.loads(adata.uns["plexora_qc"]["categories"])["classes"][
        "tissue_fold"] == "tissue_acquisition"
    flags = adata.obsm["plexora_qc_flags"]
    assert list(flags.columns) == list(schemas.REASONS)
    assert flags.loc[inside, "region:tissue_fold"].all()
    # One boolean column per marker, none set: a fold fails whole cells, it
    # does not single out a marker.
    markers = adata.obsm["plexora_qc_marker_flags"]
    assert list(markers.columns) == list(adata.var_names)
    assert not markers.fillna(False).to_numpy().any()
    assert (adata.obs.loc[inside, "plexora_qc_unreliable_markers"] == "").all()
    assert adata.uns["plexora_qc"]["result_id"]
    assert adata.uns["theirs"] == "left alone"
    refused = invoke(session, "write_qc_to_source", {"project": "qcad", "confirm": True},
                     policy=Policy.from_flags(allow_source_writes=True))
    assert refused["error"]["code"] == "conflict"
    again = invoke(session, "write_qc_to_source", {"project": "qcad", "confirm": True,
                                                   "replace": True},
                   policy=Policy.from_flags(allow_source_writes=True))
    assert again["ok"]


# -- propagation -----------------------------------------------------------------------------


def test_membership_is_by_mask_overlap_and_kept_as_a_fraction(tmp_path):
    from plexora.plugins.qc.server import propagate, strictness
    from plexora.plugins.qc.server.cells import calls

    info = make_qc_project(tmp_path, size=512, grid=16, artifacts=())
    ds = AgentSession().data("qcsynth")
    # A region whose edge runs through a column of cells: those are partly in.
    cell = next(c for c in info["cells"] if c["x"] > 200)
    edge = cell["x"]
    region = {"roi_id": "r1", "geometry": _box(0, 0, edge, 512)}
    pairs, per_roi = propagate.propagate(ds, [region])
    assert per_roi["r1"]["method"] == "mask", per_roi
    fractions = dict(zip(pairs["cell_id"].to_list(), pairs["fraction"].to_list()))
    on_edge = [c["id"] for c in info["cells"] if abs(c["x"] - edge) < 1]
    assert on_edge and all(0.3 < fractions[i] < 0.7 for i in on_edge)
    inside = [c["id"] for c in info["cells"] if c["x"] < edge - 20]
    assert all(fractions[i] > 0.99 for i in inside)
    # Lenient needs most of a cell inside, strict a quarter: the edge column
    # is out under one and in under the other.
    result = {"result_id": "r", "candidates": {"c": {"id": "c", "roi_id": "r1",
                                                     "class": "tissue_fold",
                                                     "action": "exclude"}},
              "cells": {"modules": {}}}
    failing = {}
    for preset in ("lenient", "strict"):
        frame, _p, _s = calls.derive(ds, result, strictness.thresholds(preset), pairs=pairs)
        failing[preset] = set(frame.filter(~frame["pass"])["cell_id"].to_list())
    assert set(on_edge) <= failing["strict"] and not set(on_edge) & failing["lenient"]
    assert failing["lenient"] <= failing["strict"]


def test_membership_falls_back_to_centroids_without_a_mask(tmp_path):
    from plexora.plugins.qc.server import propagate

    make_qc_project(tmp_path, size=512, grid=16, artifacts=(), mask=False)
    ds = AgentSession().data("qcsynth")
    pairs, per_roi = propagate.propagate(ds, [{"roi_id": "r1",
                                               "geometry": _box(0, 0, 200, 200)}])
    assert per_roi["r1"]["method"] == "centroid"
    assert set(pairs["method"].to_list()) == {"centroid"}


# -- the mirror script -------------------------------------------------------------------------


def test_the_mirror_script_shows_the_candidate_outline():
    from plexora.plugins.qc.server import mirror_script

    geometry = _box(10, 10, 60, 60)
    packet = {"kind": "artifact_confirm", "units": [{"project": "p", "type": "candidate",
                                                     "id": "cand_x"}],
              "evidence": {}, "images": [{"artifact_id": "art_1", "role": "confirm_sheet"}]}
    unit = {"type": "candidate", "id": "cand_x", "channel": "CD3", "bbox": [10, 10, 60, 60],
            "class_hint": "tissue_fold", "variants": {"standard": {"geometry": geometry}}}
    script = mirror_script.script_for(packet, unit, None, current_project="p")
    kinds = [c["type"] for c in script]
    assert kinds == ["set_cell_render_mode", "fit_region", "show_shapes", "show_evidence"]
    shapes = next(c for c in script if c["type"] == "show_shapes")["arguments"]["shapes"]
    assert shapes[0]["geometry"] == geometry
    assert script == mirror_script.script_for(packet, unit, None, current_project="p")
    other = mirror_script.script_for(packet, unit, None, current_project="q")
    assert other[0]["type"] == "open_project"


def test_the_viewer_command_refuses_a_shape_with_neither_geometry_nor_bounds():
    from plexora.agent.core.viewer import ShapesInput, ViewerShape

    shapes = ShapesInput(shapes=[ViewerShape(id="a")])
    with pytest.raises(AgentError):
        from plexora.agent.core import viewer

        viewer.show_shapes(None, shapes)


# -- sheets and the render ------------------------------------------------------------------------


def test_the_shapes_overlay_is_deterministic_and_in_the_manifest(tmp_path):
    from plexora.agent import render
    from plexora.agent.render_spec import Bounds, ChannelSpec, RenderInput, ShapeSpec

    make_qc_project(tmp_path, size=512, grid=16, artifacts=())
    spec = RenderInput(project="qcsynth", bounds=Bounds(x=0, y=0, width=512, height=512),
                       channels=[ChannelSpec(name="DNA_1")],
                       shapes=[ShapeSpec(id="a", geometry=_box(50, 50, 200, 200),
                                         fill_alpha=0.3, label="c1"),
                               ShapeSpec(id="b", bounds=Bounds(x=300, y=300, width=100,
                                                               height=100), dash=True)])
    first = render.render_region(AgentSession(), spec, store=False)
    second = render.render_region(AgentSession(), spec, store=False)
    assert first["png"] == second["png"]
    assert [s["id"] for s in first["manifest"]["shapes"]] == ["a", "b"]
    assert "shapes" in first["manifest"]["overlays"]
    plain = render.render_region(AgentSession(), spec.model_copy(update={"shapes": None}),
                                 store=False)
    assert plain["png"] != first["png"]


def test_the_audit_sheet_is_the_same_bytes_every_time(tmp_path):
    from plexora.plugins.qc.server import scan, sheets

    make_qc_project(tmp_path, size=512, grid=16, artifacts=())
    session = AgentSession()
    result, _ = scan.load_or_run(session, "qcsynth", params={"cell_um": 25.0})
    rows = [{"number": i + 1, "channel": c["name"], "cycle": c.get("cycle"),
             "flags": c["flags"], "candidates": []} for i, c in enumerate(result.channels)]
    one = sheets.audit_sheet(session, "qcsynth", result, rows, index=1, total=1, fmt="png",
                             pixel=None, calibration=None, store=False)
    two = sheets.audit_sheet(session, "qcsynth", result, rows, index=1, total=1, fmt="png",
                             pixel=None, calibration=None, store=False)
    assert one["image"] == two["image"]
    width, height = one["size"]
    # The budget the skill promises: about a hundred vision tokens a channel.
    from plexora.agent.sessions.budget import vision_tokens

    assert vision_tokens(width * height) / sheets.CHANNELS_PER_SHEET < 140


def test_a_panel_is_rendered_once_whatever_sheets_show_it(tmp_path):
    """The whole-tissue view of a channel is on its audit tile and again on
    each first-look row of its candidates: the second time is a cache hit,
    drawn with its own outlines on a copy."""
    from plexora.plugins.qc.server import scan, sheets

    make_qc_project(tmp_path, size=512, grid=16, artifacts=())
    session = AgentSession()
    scan._MEMORY.clear()
    sheets.clear_panel_cache()
    result, _ = scan.load_or_run(session, "qcsynth", params={"cell_um": 25.0})
    rows = [{"number": i + 1, "channel": c["name"], "cycle": c.get("cycle"),
             "flags": c["flags"], "candidates": []} for i, c in enumerate(result.channels)]
    first = sheets.audit_sheet(session, "qcsynth", result, rows, index=1, total=1, fmt="png",
                               pixel=None, calibration=None, store=False)
    drawn = sheets.PANEL_STATS["misses"]
    assert drawn == len(rows) + 1 and sheets.PANEL_STATS["hits"] == 0
    again = sheets.audit_sheet(session, "qcsynth", result, rows, index=1, total=1, fmt="png",
                               pixel=None, calibration=None, store=False)
    assert sheets.PANEL_STATS["misses"] == drawn
    assert sheets.PANEL_STATS["hits"] == len(rows) + 1
    assert again["image"] == first["image"]
    mask = np.zeros(tuple(result.grid["shape"]), dtype=bool)
    mask[4:7, 4:7] = True
    s = result.grid["cell_full_px"]
    unit = {"id": "cand_x", "label": "c1", "channels": ["CD3"], "class_hint": "tissue_fold",
            "bbox": [4 * s, 4 * s, 7 * s, 7 * s], "primary_metric": "CD3::median",
            "geometry": _box(4 * s, 4 * s, 7 * s, 7 * s)}
    hits = sheets.PANEL_STATS["hits"]
    batch = sheets.confirm_batch_sheet(session, "qcsynth", result, [unit, dict(unit, id="y")],
                                       [mask, mask], fmt="png", pixel=None, calibration=None,
                                       store=False)
    # CD3's whole-tissue tile came from the audit; the second row repeats the first.
    assert sheets.PANEL_STATS["hits"] >= hits + 1 + 3
    assert len(batch["manifest"]["rows"]) == 2


def test_one_cutoff_step_moves_it_visibly():
    """A too_lenient answer moves a cutoff by at least a MAD and a quarter of
    its distance from the median: cycle stability held out by its floor
    (tiny MAD) used to move 0.011 in log10 ratio per step."""
    from plexora.plugins.qc.server import strictness
    from plexora.plugins.qc.server.cells import modules

    rng = np.random.default_rng(0)
    ratio = rng.normal(-0.15, 0.015, size=5000)
    meas = {"m_cycle_log10_ratio": ratio}
    table = strictness.thresholds("standard")
    module = modules.module("cycle_stability")
    base = module.cutoffs(meas, table)
    moved = module.cutoffs(meas, table, {"low": {"offset_steps": 1}})
    spread = base["median"] - base["low"]
    assert moved["low"] - base["low"] >= 0.25 * spread - 1e-9
    assert moved["low"] - base["low"] > 5 * base["mad"]
    # Never closer to the median than the floor share of its distance.
    far = module.cutoffs(meas, table, {"low": {"offset_steps": 2}})
    assert base["median"] - far["low"] >= 0.4 * spread - 1e-9
    values = rng.normal(5.0, 0.25, size=20000)
    outlier = modules.module("channel_outlier:CD3")
    # One population and no positives: nothing to be far beyond, no cutoff.
    assert outlier.cutoffs({"m_outlier_log": values}, table)["reference"] == "none"
    marker = np.concatenate([values, rng.normal(7.5, 0.4, size=4000)])
    out = {"m_outlier_log": marker}
    one = outlier.cutoffs(out, table)
    # Measured against the positives: the positive population is not the outlier.
    assert one["reference"] == "positive cells"
    assert (marker > one["high"]).sum() < 0.01 * 4000
    two = outlier.cutoffs(out, table, {"high": {"offset_steps": 1}})
    assert one["high"] - two["high"] >= one["mad"] - 1e-9
    assert (marker > two["high"]).sum() >= (marker > one["high"]).sum()
    # Presets stay nested after the same moves.
    for steps in (-2, -1, 0, 1, 2):
        decision = {"low": {"offset_steps": steps}, "high": {"offset_steps": steps}}
        cuts = [modules.module("segmentation_area").cutoffs(
            {"m_area_log": values}, strictness.thresholds(p), decision)
            for p in ("lenient", "standard", "strict")]
        assert cuts[0]["low"] <= cuts[1]["low"] <= cuts[2]["low"]
        assert cuts[0]["high"] >= cuts[1]["high"] >= cuts[2]["high"]


def test_a_module_with_nothing_beyond_or_near_its_cutoffs_is_accepted_unseen():
    from plexora.plugins.qc.server.cells import bulk, modules

    values = np.concatenate([np.full(1000, 5.0), [4.0, 6.0]])
    cutoffs = {"low": 3.0, "high": 7.0, "step": {"low": 0.5, "high": 0.5}}
    at = {side: dict(zip(("beyond", "near"), modules.beyond_and_near(values, cutoffs, side)))
          for side in ("low", "high")}
    assert at == {"low": {"beyond": 0, "near": 0}, "high": {"beyond": 0, "near": 0}}
    assert bulk._nothing_to_show(at)
    near = {"low": {"beyond": 0, "near": 40}, "high": {"beyond": 0, "near": 0}}
    assert not bulk._nothing_to_show(near)
    assert not bulk._nothing_to_show({"high": {"beyond": 3, "near": 0}})


def test_a_confirm_answer_is_one_verdict_or_verdicts_by_label():
    from pydantic import ValidationError

    from plexora.plugins.qc.server import answers

    single = answers.ArtifactConfirmAnswer(verdict="not_artifact")
    assert list(single.judgments(["c4"])) == ["c4"]
    batch = answers.ArtifactConfirmAnswer(verdicts={
        "c1": {"verdict": "artifact", "artifact_class": "tissue_fold", "severity": "minor"},
        "c2": {"verdict": "need_more_evidence"}})
    assert batch.judgments(["c1", "c2"])["c1"].artifact_class == "tissue_fold"
    for bad in ({}, {"verdict": "artifact", "verdicts": {"c1": {"verdict": "artifact"}}}):
        with pytest.raises(ValidationError):
            answers.ArtifactConfirmAnswer(**bad)
    modules = answers.CellModulesAnswer(modules={"cycle_stability": {"low": "too_lenient",
                                                                     "pattern": "tissue_loss"}})
    assert modules.modules["cycle_stability"].high == "accept"
    assert "verdicts" in answers.schema_for("artifact_confirm")["properties"]
    assert "modules" in answers.schema_for("cell_modules")["properties"]


# -- small rules -------------------------------------------------------------------------------------


def test_a_custom_strictness_outside_the_band_is_refused():
    from plexora.plugins.qc.server import strictness

    with pytest.raises(AgentError):
        strictness.thresholds("custom", {"area.k": 10.0})
    with pytest.raises(AgentError):
        strictness.thresholds("custom", {"not.a.key": 1.0})
    table = strictness.thresholds("custom", {"area.k": 3.2})
    assert table["area.k"] == 3.2


@pytest.mark.parametrize("names,method,groups", [
    (["DNA_1", "CD3", "CD8", "DNA_2", "CD20", "CD4"], "nuclear_repeats",
     [["DNA_1", "CD3", "CD8"], ["DNA_2", "CD20", "CD4"]]),
    (["CD3_c1", "CD8_c1", "CD20_c2", "CD4_c2"], "name_suffix",
     [["CD3_c1", "CD8_c1"], ["CD20_c2", "CD4_c2"]]),
    (["CD3", "CD8", "CD20"], "none", []),
])
def test_cycles_are_inferred_from_the_names(names, method, groups):
    from plexora.plugins.qc.server import cycles

    found = cycles.infer(names)
    assert found["method"] == method
    assert [c["channels"] for c in found["cycles"]] == groups


def test_a_cycle_override_wins():
    from plexora.plugins.qc.server import cycles

    found = cycles.infer(["A", "B", "C", "D"], override={"period": 2})
    assert found["method"] == "user"
    assert [c["channels"] for c in found["cycles"]] == [["A", "B"], ["C", "D"]]


def test_the_vocabulary_is_closed():
    from plexora.plugins.qc.server import schemas

    assert len(set(schemas.ARTIFACT_CLASSES)) == len(schemas.ARTIFACT_CLASSES) == 21
    assert set(schemas.CLASS_WORDS) == set(schemas.ARTIFACT_CLASSES)
    assert set(schemas.CLASS_COLORS) == set(schemas.ARTIFACT_CLASSES)
    assert set(schemas.PRIMARY_ORDER) == set(schemas.REASONS)
    assert set(schemas.CLASS_CATEGORY) == set(schemas.ARTIFACT_CLASSES)
    assert set(schemas.CELL_REASON_CATEGORY) >= set(schemas.REASONS) | set(
        schemas.MARKER_REASONS)
    assert "tissue_artifact" not in schemas.AGENT_CLASSES
    assert "segmentation_error" in schemas.AGENT_CLASSES
    for action in schemas.ACTIONS:
        assert schemas.action_of_name(schemas.roi_name(action, "tissue_fold", ["CD3"])) == action


def test_every_finding_maps_to_one_of_five_categories():
    from plexora.plugins.qc.server import schemas

    assert schemas.CATEGORY_IDS == ("blur_focus", "registration", "segmentation",
                                    "tissue_acquisition", "staining_signal")
    assert schemas.category_of_class("out_of_focus") == "blur_focus"
    assert schemas.category_of_class("stitching_or_tile_seam") == "tissue_acquisition"
    assert schemas.category_of_class("antibody_aggregate") == "staining_signal"
    assert schemas.category_of_class("uncertain_manual_review") == "review"
    assert schemas.category_of_reason("seg_large") == "segmentation"
    assert schemas.category_of_reason("area_small") == "segmentation"
    assert schemas.category_of_reason("cycle_gain") == "registration"
    assert schemas.category_of_reason("region:tissue_fold") == "tissue_acquisition"
    for category in (*schemas.CATEGORY_IDS, "review"):
        category_id = schemas.roi_category_id(category)
        label = schemas.roi_category_label(category)
        assert category_id == f"qc_{category}"
        assert schemas.category_key(category_id) == category
        assert schemas.category_of_label(label) == category
        assert schemas.class_of_category(category_id) == schemas.default_class(category)
        assert schemas.class_of_label(label) == schemas.default_class(category)
        assert not schemas.is_legacy_label(label)
    # A class stands for its category wherever a category is named.
    assert schemas.roi_category_id("tissue_fold") == "qc_tissue_acquisition"
    assert schemas.roi_category_label("out_of_focus") == "QC: Blur / focus issue"
    # A category QC wrote before the five is still read, as its class.
    assert schemas.class_of_category("qc_tissue_fold") == "tissue_fold"
    assert schemas.category_key("qc_tissue_fold") == "tissue_acquisition"
    assert schemas.class_of_label("QC: Tissue fold") == "tissue_fold"
    assert schemas.is_legacy_label("QC: Tissue fold")
    # A custom one is a technical artifact in the tissue category.
    key = schemas.custom_key("Pen mark")
    assert schemas.category_key(schemas.roi_category_id(key)) == key
    assert schemas.class_of_category(schemas.roi_category_id(key)) == "other_technical"
    assert schemas.category_key("qc_nonsense") is None
    assert schemas.category_key("cat_123") is None
    help_rows = schemas.public_categories()
    assert [c["id"] for c in help_rows] == list(schemas.CATEGORY_IDS)
    assert all(c["help"] and c["classes"] for c in help_rows)


def test_a_traced_region_is_judged_large_by_what_it_removes():
    """The large-region rule reads the trace's share of the tissue; the area
    floor stays on the envelope the agent judged."""
    from plexora.plugins.qc.server import schemas, strictness

    table = strictness.thresholds("standard")
    decision = {"artifact_class": "tissue_fold", "severity": "severe", "confidence": "sure"}
    large = schemas.ENGINE["large_region_fraction"]
    envelope = {"tissue_fraction": large + 0.1}
    assert strictness.decide_artifact(decision, envelope, table)["action"] == "warn"
    traced = {"tissue_fraction": large + 0.1, "refined_fraction": 0.05}
    assert strictness.decide_artifact(decision, traced, table)["action"] == "exclude"
    floor = table["artifact.min_area_fraction_exclude"]
    specks = {"tissue_fraction": max(floor, 0.01), "refined_fraction": floor / 100}
    assert strictness.decide_artifact(decision, specks, table)["action"] == "exclude"


def test_clip_to_keeps_what_is_inside_and_nothing_else():
    from shapely.geometry import shape

    from plexora.plugins.qc.server import polygons

    def box(x0, y0, x1, y1):
        return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                    [x0, y0]]]}

    assert polygons.clip_to(box(0, 0, 10, 10), box(20, 20, 30, 30)) is None
    # Touching along an edge is not overlap.
    assert polygons.clip_to(box(0, 0, 10, 10), box(10, 0, 20, 10)) is None
    clipped = polygons.clip_to(box(0, 0, 10, 10), box(5, 5, 30, 30))
    assert shape(clipped).area == pytest.approx(25.0)
    grid = {"shape": [4, 4], "cell_full_px": 10.0, "image_size": [40, 40]}
    small = box(12, 12, 14, 14)
    touched = polygons.geometry_to_grid(small, grid, touch=True)
    assert touched.sum() == 1 and touched[1, 1]
