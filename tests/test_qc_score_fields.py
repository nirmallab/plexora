"""The image checks' scores as one field: thresholds, regions, strata, sheets."""

import numpy as np
import pytest


def _field(values, *, step=10.0, pixel_um=1.0, check="blur", auto=0.5, weight=None):
    from plexora.plugins.qc.server import score_fields

    values = np.asarray(values, dtype=np.float64)
    ny, nx = values.shape
    return score_fields.ScoreField(
        check=check, values=values,
        weight=np.ones_like(values) if weight is None else weight,
        grid={"x0": 0.0, "y0": 0.0, "step": step, "nx": nx, "ny": ny,
              "image_size": [nx * step, ny * step]},
        fingerprint="fp", auto_threshold=auto, cell_um=step * pixel_um, pixel_um=pixel_um)


def _scene(seed=0):
    rng = np.random.default_rng(seed)
    values = rng.uniform(0.05, 0.25, size=(40, 40))
    values[5:15, 5:15] = rng.uniform(0.7, 0.9, size=(10, 10))
    values[30:33, 30:33] = 0.55
    values[0, :] = np.nan
    return values


def test_a_bar_moves_in_steps_and_tighter_flags_more():
    from plexora.plugins.qc.server import schemas, score_fields

    field = _field(_scene())
    auto = score_fields.bar(field)
    tighter = score_fields.bar(field, 1)
    looser = score_fields.bar(field, -1)
    assert auto["offset_steps"] == 0 and auto["value"] == 0.5
    assert tighter["value"] < auto["value"] < looser["value"]
    assert tighter["value"] == pytest.approx(auto["value"] - auto["step"])
    limit = schemas.ENGINE["adjust_max_steps"]
    assert score_fields.bar(field, 99)["offset_steps"] == limit
    assert score_fields.bar(field, -99)["offset_steps"] == -limit
    found_auto = score_fields.regions(field, auto["value"])
    found_tight = score_fields.regions(field, tighter["value"], min_cells=1)
    assert found_tight["flagged_pct"] >= found_auto["flagged_pct"]


def test_regions_are_the_flagged_cells_largest_first_with_their_denominator():
    from plexora.plugins.qc.server import score_fields

    field = _field(_scene())
    found = score_fields.regions(field, 0.5, min_cells=4)
    assert found["n_regions"] == 2 and found["denominator"] == "evaluable_tissue"
    big, small = found["regions"]
    assert big["cells"] == 100 and small["cells"] == 9
    assert big["bbox"] == [50.0, 50.0, 150.0, 150.0]
    assert big["geometry"]["type"] in ("Polygon", "MultiPolygon")
    # 109 flagged of 39 x 40 evaluable cells.
    assert found["flagged_pct"] == pytest.approx(100.0 * 109 / (39 * 40), abs=0.01)
    assert score_fields.regions(field, 0.5, min_cells=10)["n_regions"] == 1


def test_strata_sample_across_the_distribution_deterministically_and_spaced():
    from plexora.plugins.qc.server import score_fields

    field = _field(_scene())
    bar = score_fields.bar(field)
    found = score_fields.regions(field, bar["value"])
    rows = score_fields.sample_strata(field, bar["value"], bar["step"], found)
    again = score_fields.sample_strata(field, bar["value"], bar["step"], found)
    assert rows == again
    assert list(rows)[0] == "clear_good" or "clear_good" in rows
    for place in rows["strongly_abnormal"]:
        assert place["score"] >= bar["value"] + bar["step"]
    for place in rows.get("clear_good", []):
        assert place["score"] < bar["value"] - bar["step"]
    for place in rows.get("borderline_above", []):
        assert bar["value"] <= place["score"] < bar["value"] + bar["step"]
    spacing = score_fields.spacing_px(field)
    for places in rows.values():
        for i, a in enumerate(places):
            for b in places[i + 1:]:
                assert abs(a["x"] - b["x"]) >= spacing or abs(a["y"] - b["y"]) >= spacing
    assert rows["clustered"][0]["region"] == found["regions"][0]["id"]
    seen = [tuple(p["cell"]) for places in rows.values() for p in places]
    assert len(seen) == len(set(seen))


def test_the_blur_evaluation_is_the_shared_one(tmp_path):
    """Blur QC thresholds its scores through score_fields: the same regions."""
    from plexora.agent import AgentSession
    from plexora.plugins.qc.server import blur, score_fields
    from tests.qc_fixtures import make_qc_project

    make_qc_project(tmp_path, artifacts=("blur_local",))
    summary, _ = blur.load_or_run(AgentSession(), "qcsynth", channel="DNA_2")
    arrays = blur.load_arrays("qcsynth", summary["fingerprint"])
    evaluated = blur.evaluate(arrays, summary, summary["auto_threshold"])
    found = score_fields.regions(score_fields.from_blur(summary, arrays),
                                 summary["auto_threshold"], min_cells=blur.MIN_REGION_TILES)
    assert evaluated["blurred_pct"] == found["flagged_pct"]
    assert [r["tiles"] for r in evaluated["regions"]] == [r["cells"] for r in found["regions"]]
    assert (evaluated["mask"] == found["mask"]).all()


def test_a_blur_review_shows_rows_from_fine_to_far_above(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.qc.server import blur, score_review
    from tests.qc_fixtures import make_qc_project

    make_qc_project(tmp_path, artifacts=("blur_local",))
    session = AgentSession()
    blur.load_or_run(session, "qcsynth", channel="DNA_2")
    field = score_review.field_for(session, "qcsynth", "blur", channel="DNA_2")
    look = score_review.review(session, "qcsynth", field, store=False)
    assert look["found"]["n_regions"] >= 1
    assert "strongly_abnormal" in look["strata"] or "clustered" in look["strata"]
    assert look["evidence"]["threshold"]["source"] == "auto"
    width, height = look["sheet"]["size"]
    assert width <= 960 and height <= 1100
    rows = look["sheet"]["manifest"]["rows"]
    assert rows[-1]["stratum"] == "global"
    manifest = score_review.manifest_of(look)
    assert all("position" in m or m.get("kind") for m in manifest)
    same = score_review.review(session, "qcsynth", field, store=False)
    assert same["strata"] == look["strata"]
