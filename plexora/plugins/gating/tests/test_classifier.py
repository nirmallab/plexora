"""The Single Cell Classifier's server half, end to end on a synthetic project.

The project (tests/agent_fixtures.make_synthetic_project) has an image, a mask
and a table with known groups: CD8 negative, positive and borderline cells on
an 8x8 grid. The claims: a run trains on clicked examples and calls every
eligible cell; a rerun replaces the run whole; the eraser goes back to the
baseline and keeps the examples; refusals change nothing; QC exclusions are
re-applied on every read; the files round-trip.
"""

from __future__ import annotations

import csv
import io
import json

import numpy as np
import pytest

from plexora.plugins.gating.server.classifier import (
    VIEW_NOT_CLASSIFIED, VIEW_POSITIVE, ClassifierRefused, calls, export, features, panel,
    runs, store)
from tests.agent_fixtures import local_handles, make_synthetic_project


@pytest.fixture
def made(tmp_path):
    return make_synthetic_project(tmp_path)


@pytest.fixture
def ds(made):
    return local_handles(made["name"])


def _label(ds, made, n_pos=4, n_neg=4):
    for cell in made["positives"][:n_pos]:
        panel.annotate(ds, "CD8", cell, "positive")
    for cell in made["negatives"][:n_neg]:
        panel.annotate(ds, "CD8", cell, "negative")


def _row_of(ds, cell_id):
    from plexora.plugins.gating.server.autogate.cells import cells

    return int(np.flatnonzero(cells(ds).ids == cell_id)[0])


# -- annotating ---------------------------------------------------------------


def test_a_click_labels_a_second_click_removes_the_other_label_flips(ds, made):
    cell = made["positives"][0]
    first = panel.annotate(ds, "CD8", cell, "positive")
    assert first["after"] == "positive" and first["examples"]["n_positive"] == 1
    # The example carries a position for the overlay.
    spot = first["examples"]["positive"][0]
    assert spot["cell_id"] == cell and spot["x"] > 0 and spot["y"] > 0
    flipped = panel.annotate(ds, "CD8", cell, "negative")
    assert flipped["before"] == "positive" and flipped["after"] == "negative"
    assert flipped["examples"]["n_positive"] == 0 and flipped["examples"]["n_negative"] == 1
    removed = panel.annotate(ds, "CD8", cell, "negative")
    assert removed["after"] is None and removed["examples"]["n_negative"] == 0


def test_a_cell_the_table_does_not_have_is_refused(ds):
    with pytest.raises(ClassifierRefused):
        panel.annotate(ds, "CD8", 999_999, "positive")
    with pytest.raises(ClassifierRefused):
        panel.annotate(ds, "not_a_marker", 1, "positive")


def test_cell_at_reads_the_mask_and_falls_back_nearby(ds, made):
    cell = made["cells"][9]
    hit = panel.cell_at(ds, cell["x"], cell["y"])
    assert hit["cell_id"] == cell["id"] and hit["method"] == "mask"
    # Just outside the disc (radius 18): the nearest labelled pixel.
    near = panel.cell_at(ds, cell["x"] + 21, cell["y"], radius=8)
    assert near["cell_id"] == cell["id"] and near["method"] == "mask_nearby"
    # Far from every cell.
    assert panel.cell_at(ds, cell["x"] + 32, cell["y"] + 32, radius=4)["cell_id"] is None


# -- running ------------------------------------------------------------------


def test_too_few_examples_is_refused_and_changes_nothing(ds, made):
    _label(ds, made, n_pos=2, n_neg=5)
    with pytest.raises(ClassifierRefused, match="3 positive"):
        runs.run(ds, "CD8")
    assert store.run_record(ds.name, "CD8") is None
    assert calls.active_source(ds, "CD8") == "threshold"


def test_a_run_calls_every_eligible_cell(ds, made):
    _label(ds, made)
    steps = []
    record = runs.run(ds, "CD8", progress=lambda d, t, m: steps.append(m))
    assert [s for s in runs.STAGES if s in steps] == [s for s in runs.STAGES if s != "pick"]
    assert "pixels" in record["feature_groups"] and "nuclear" in record["feature_groups"]
    assert record["n_eligible"] == len(made["cells"])
    assert calls.active_source(ds, "CD8") == "classifier"
    codes, info = calls.viewer_codes(ds, "CD8")
    assert info["state"] == "ready" and info["eligible"] == len(made["cells"])
    for cell in made["positives"]:
        assert codes[_row_of(ds, cell)] in (VIEW_POSITIVE, 3), cell
    called, source = calls.marker_calls(ds, "CD8")
    assert source == "classifier"
    assert all(called[_row_of(ds, c)] == 0 for c in made["negatives"])
    summary = calls.summary(ds, "CD8")
    assert summary["source"] == "classifier" and summary["n_positive"] >= len(made["positives"])


def test_a_labelled_cell_is_called_as_it_was_labelled(ds, made):
    _label(ds, made)
    # A borderline cell labelled negative stays negative whatever the model says.
    border = made["borderline"][0]
    panel.annotate(ds, "CD8", border, "positive")
    runs.run(ds, "CD8")
    called, _ = calls.marker_calls(ds, "CD8")
    assert called[_row_of(ds, border)] == 1


def test_a_rerun_replaces_the_run_whole(ds, made):
    _label(ds, made)
    first = runs.run(ds, "CD8")
    panel.annotate(ds, "CD8", made["borderline"][1], "negative")
    second = runs.run(ds, "CD8")
    assert second["run_id"] != first["run_id"]
    assert not store.run_path(ds.name, "CD8", first["run_id"]).exists()
    assert store.run_path(ds.name, "CD8", second["run_id"]).exists()
    assert store.run_record(ds.name, "CD8")["run_id"] == second["run_id"]
    assert second["n_examples_negative"] == first["n_examples_negative"] + 1


def test_a_cancelled_run_keeps_the_previous_one(ds, made):
    _label(ds, made)
    first = runs.run(ds, "CD8")
    asked = {"n": 0}

    def cancel_at_store():
        asked["n"] += 1
        return asked["n"] > 4

    with pytest.raises(InterruptedError):
        runs.run(ds, "CD8", cancelled=cancel_at_store)
    assert store.run_record(ds.name, "CD8")["run_id"] == first["run_id"]
    assert store.read_arrays(ds.name, "CD8", first["run_id"]) is not None


def test_the_eraser_restores_the_baseline_and_keeps_examples(ds, made):
    from plexora.plugins.gating.server import model

    model.set_gate(ds, "CD8", 400.0, 1e9)
    _label(ds, made)
    runs.run(ds, "CD8")
    runs.run(ds, "CD8")
    state = store.read_state(ds.name)["markers"]["CD8"]
    assert state["baseline"]["active"] == "threshold"
    assert state["baseline"]["gate"]["low"] == 400.0
    result = runs.revert(ds, "CD8")
    assert result == {"reverted": True, "active": "threshold", "discarded": False}
    assert calls.active_source(ds, "CD8") == "threshold"
    assert store.run_record(ds.name, "CD8") is None
    assert store.examples(ds.name, "CD8") == ([*made["positives"][:4]], [*made["negatives"][:4]])
    # Thresholding's calls are back, from the gate the slider left.
    called, source = calls.marker_calls(ds, "CD8")
    assert source == "threshold" and called[_row_of(ds, made["positives"][0])] == 1
    assert model.get_gate(ds, "CD8")["low"] == 400.0
    # The next run captures a fresh baseline.
    runs.run(ds, "CD8")
    assert store.read_state(ds.name)["markers"]["CD8"]["baseline"] is not None


def test_reset_reverts_every_marker_and_forgets_examples(ds, made):
    _label(ds, made)
    runs.run(ds, "CD8")
    result = runs.reset(ds)
    assert result["reverted"] == ["CD8"] and result["examples_cleared"] == 8
    assert store.counts(ds.name) == {}


def test_qc_exclusions_are_reapplied_on_every_read(ds, made, monkeypatch):
    _label(ds, made)
    runs.run(ds, "CD8")
    row = _row_of(ds, made["positives"][2])
    skip = np.zeros(len(made["cells"]), dtype=bool)
    skip[row] = True
    monkeypatch.setattr(runs, "qc_state", lambda ds, marker: (skip, "qc:1", "strict"))
    codes, info = calls.viewer_codes(ds, "CD8")
    assert codes[row] == VIEW_NOT_CLASSIFIED
    assert info["eligible"] == len(made["cells"]) - 1
    assert runs.stale_reason(ds, store.run_record(ds.name, "CD8")) == "QC changed since this run"


def test_a_table_that_changed_is_stale_and_not_drawn(ds, made, monkeypatch):
    _label(ds, made)
    runs.run(ds, "CD8")
    monkeypatch.setattr(features, "table_fingerprint", lambda ds: "different")
    codes, info = calls.viewer_codes(ds, "CD8")
    assert info["state"] == "stale" and (codes == VIEW_NOT_CLASSIFIED).all()
    assert runs.stale_reason(ds, store.run_record(ds.name, "CD8")).startswith("the table")


def test_table_features_alone_without_a_mask(tmp_path):
    made = make_synthetic_project(tmp_path, name="nomask", mask=False)
    ds = local_handles("nomask")
    _label(ds, made)
    record = runs.run(ds, "CD8")
    assert record["feature_groups"] == ["table"]
    assert record["pixel_reason"] == "no segmentation mask"
    # Clicks fall back to the nearest centroid.
    cell = made["cells"][3]
    assert panel.cell_at(ds, cell["x"] + 2, cell["y"])["method"] == "centroid"


def test_pixel_features_are_cached(ds, made, monkeypatch):
    _label(ds, made)
    runs.run(ds, "CD8")
    from plexora.server.utils import cell_features

    def boom(*args, **kwargs):
        raise AssertionError("pixels read twice")

    monkeypatch.setattr(cell_features, "accumulate", boom)
    record = runs.run(ds, "CD8")
    assert record["pixels"]["where"] == "cache"


# -- files ----------------------------------------------------------------------


def test_the_two_csvs_are_wide_and_aligned(ds, made):
    _label(ds, made)
    runs.run(ds, "CD8")
    for kind in ("binary", "scores"):
        markers, chunks = export.stream_csv(ds, kind)
        rows = list(csv.reader(io.StringIO("".join(chunks))))
        assert markers == ["CD8"]
        assert rows[0] == ["image_id", "cell_id", "CD8"]
        assert len(rows) == 1 + len(made["cells"])
        by_id = {int(r[1]): r[2] for r in rows[1:]}
        positive = made["positives"][0]
        if kind == "binary":
            assert by_id[positive] == "1" and by_id[made["negatives"][0]] == "0"
        else:
            assert float(by_id[positive]) > 0.5


def test_annotations_round_trip_with_merge_and_replace(ds, made):
    _label(ds, made)
    document = export.annotations_document(ds)
    assert document["format"] == export.FORMAT
    assert sorted(document["markers"]["CD8"]["positive"]) == sorted(made["positives"][:4])
    store.clear_examples(ds.name)
    report = export.preview(ds, json.loads(json.dumps(document)))
    assert report["markers"]["CD8"]["valid_positive"] == 4
    assert report["warnings"] == []
    export.apply(ds, report["token"], "replace")
    assert store.counts(ds.name)["CD8"] == (4, 4)
    # Merge: the file's labels win, the rest stay.
    extra = {**document, "markers": {"CD8": {"positive": [made["negatives"][0]],
                                             "negative": []}}}
    token = export.preview(ds, extra)["token"]
    export.apply(ds, token, "merge")
    pos, neg = store.examples(ds.name, "CD8")
    assert made["negatives"][0] in pos and len(neg) == 3 and len(pos) == 5


def test_an_import_reports_problems_and_applies_nothing_until_asked(ds, made):
    document = {"format": export.FORMAT, "schema_version": 1,
                "markers": {"CD8": {"positive": [made["positives"][0], 123456, 123456],
                                    "negative": [made["positives"][0], made["negatives"][0]]},
                            "Nope": {"positive": [1], "negative": []}}}
    report = export.preview(ds, document)
    cd8 = report["markers"]["CD8"]
    assert cd8 == {"valid_positive": 0, "valid_negative": 1, "unknown_cells": 1,
                   "duplicates": 1, "conflicts": 1}
    assert any("Nope" in p for p in report["problems"])
    assert store.counts(ds.name) == {}
    with pytest.raises(ClassifierRefused):
        export.preview(ds, {"format": "something else"})
    with pytest.raises(ClassifierRefused, match="expired"):
        export.apply(ds, "deadbeef", "merge")


def test_the_mode_is_project_state(ds):
    assert panel.state(ds)["mode"] == "threshold"
    panel.set_mode(ds, "classifier")
    assert panel.state(ds)["mode"] == "classifier"
    with pytest.raises(ClassifierRefused):
        panel.set_mode(ds, "other")


# -- the wand: examples picked, and how sure the calls are ----------------------------


def test_the_wand_picks_clear_examples_across_the_image(ds, made):
    from plexora.plugins.gating.server.classifier import autopick

    # Ten a class: the 64-cell grid has too few clear cells for forty.
    pos, neg, info = autopick.pick(ds, "CD8", recipe=autopick.Recipe(count=10))
    assert len(pos) == len(neg) == 10
    assert set(pos) <= set(made["positives"]), "a picked positive is a positive cell"
    assert set(neg) <= set(made["negatives"]) | set(made["borderline"]) - set(made["positives"])
    assert not set(pos) & set(made["borderline"]) or info["how"] == "ranks"
    # Spread: the picks are not one corner of the grid.
    rows = {int((c["y"] // 64)) for c in made["cells"] if c["id"] in pos}
    assert len(rows) >= 4
    # The same project picks the same cells again.
    assert autopick.pick(ds, "CD8", recipe=autopick.Recipe(count=10))[0] == pos
    # The first wand's picks, kept as FIRST, are still what it picked.
    first, _neg, _info = autopick.pick(ds, "CD8", recipe=autopick.FIRST)
    assert len(first) == 10 and set(first) <= set(made["positives"])


class _Table:
    log_transformed = False

    def __init__(self, values):
        self.values = values
        self.markers = ["M"]

    def columns(self, names):
        return {"M": self.values}


class _Project:
    """Only what autopick reads: a table, and (patched) geometry, QC and gate."""

    name = "fake"

    def __init__(self, values):
        self.table = _Table(values)


def _scene():
    """Five places on one image, far enough apart that a cell's six nearest
    neighbours are its own place's:

    - 30 clear positives (9-11), alone;
    - 10 glow-like cells (12-14) each beside a bright one (40);
    - 10 dim cells (3.5) each beside a positive (10): the hard negatives;
    - 60 ordinary negatives (0.5-2.5).
    The gate is 5; the pixels say what the table says.
    """
    rng = np.random.default_rng(0)
    xs, ys, values, kind = [], [], [], []

    def add(x, y, value, what):
        xs.append(x), ys.append(y), values.append(value), kind.append(what)

    for i in range(30):
        add(100 * (i % 6), 100 * (i // 6), rng.uniform(9, 11), "positive")
    for i in range(10):
        x, y = 5000 + 100 * (i % 5), 100 * (i // 5)
        add(x, y, rng.uniform(12, 14), "glow")
        add(x + 6, y, 40.0, "bright")
    for i in range(10):
        x, y = 10000 + 100 * (i % 5), 100 * (i // 5)
        add(x, y, 3.5, "dim_beside_bright")
        add(x + 6, y, 10.0, "positive_partner")
    for i in range(60):
        add(15000 + 100 * (i % 10), 100 * (i // 10), rng.uniform(0.5, 2.5), "negative")
    values = np.asarray(values)
    matrix = np.column_stack([np.log1p(values), values - 1.0]).astype(np.float32)
    return (np.asarray(xs, float), np.asarray(ys, float), values, np.asarray(kind),
            matrix, ["px_mean", "px_contrast"])


@pytest.fixture
def scene(monkeypatch):
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.classifier import geometry

    xs, ys, values, kind, matrix, names = _scene()
    n = values.size
    shape = geometry.Cells(np.arange(1, n + 1, dtype=np.int64), xs, ys,
                           np.ones(n, dtype=bool), None)
    monkeypatch.setattr(geometry, "cells", lambda ds: shape)
    monkeypatch.setattr(runs, "qc_state", lambda ds, marker: (None, "", "qc"))
    monkeypatch.setattr(model, "gmm_for", lambda ds, marker: {"gate": 5.0})
    return _Project(values), kind, matrix, names


def _kinds(ids, kind):
    return {str(kind[i - 1]) for i in ids}


def test_the_wand_passes_over_glow_and_picks_hard_negatives(scene):
    from plexora.plugins.gating.server.classifier import autopick

    project, kind, matrix, names = scene
    recipe = autopick.Recipe(count=9)
    pos, neg, info = autopick.pick(project, "M", recipe=recipe, matrix=matrix, names=names)
    # A cell lit by a neighbour twice as bright is never a positive.
    assert "glow" not in _kinds(pos, kind)
    # A third of the negatives sit dim beside a cell reading positive.
    assert info["glow_test"] == "pixels" and info["hard_negatives"] == 3
    assert [str(kind[i - 1]) for i in neg].count("dim_beside_bright") == 3
    assert _kinds(neg, kind) <= {"negative", "dim_beside_bright"}
    # By value alone (the first wand) the glow cells are fair game, and no
    # negative is a hard one.
    seen = set()
    for seed in range(4):
        pos, neg, _ = autopick.pick(project, "M", seed=seed, recipe=autopick.Recipe(
            count=9, agree=False, glow=False, hard_share=0.0))
        seen |= _kinds(pos, kind)
        assert "dim_beside_bright" not in _kinds(neg, kind)
    assert "glow" in seen


def test_without_pixels_or_with_a_logged_table_there_is_no_glow_test(scene):
    from plexora.plugins.gating.server.classifier import autopick

    project, _kind, _matrix, _names = scene
    # The table's own values stand in for the pixels when they are not logged...
    _pos, _neg, info = autopick.pick(project, "M", recipe=autopick.Recipe(count=9))
    assert info["glow_test"] == "table" and info["hard_negatives"] == 3
    # ...and when they are, a ratio of them means nothing: no glow test.
    project.table.log_transformed = True
    _pos, _neg, info = autopick.pick(project, "M", recipe=autopick.Recipe(count=9))
    assert info["glow_test"] is None and info["hard_negatives"] == 0


def test_a_wand_run_keeps_a_persons_labels_and_replaces_its_own(ds, made):
    border = made["borderline"][0]
    panel.annotate(ds, "CD8", border, "negative")
    record = runs.run(ds, "CD8", auto_pick=True)
    assert record["auto_pick"]["how"] in ("gate", "ranks")
    origins = store.origins(ds.name, "CD8")
    assert origins[border] == "click"
    n_auto = sum(1 for o in origins.values() if o == "auto")
    assert n_auto >= 6
    # The person's one label is the one example given; the wand's weigh less.
    assert record["n_examples_given"] == 1
    payload = panel.examples_payload(ds, "CD8")
    assert payload["n_auto"] == n_auto
    assert any(e["auto"] for e in payload["positive"])
    assert not next(e for e in payload["negative"] if e["cell_id"] == border)["auto"]
    # A second press replaces the wand's picks, never adds to them.
    runs.run(ds, "CD8", auto_pick=True)
    assert sum(1 for o in store.origins(ds.name, "CD8").values() if o == "auto") == n_auto
    called, _ = calls.marker_calls(ds, "CD8")
    assert called[_row_of(ds, border)] == 0


def test_uncertainty_comes_from_resampled_models_and_narrows_with_examples(ds, made):
    from plexora.plugins.gating.server.classifier import learner

    _label(ds, made, n_pos=3, n_neg=3)
    few = runs.run(ds, "CD8")
    _record, arrays = calls.run_arrays(ds, "CD8")
    agreement = arrays["agreement"]
    assert agreement is not None and agreement.max() <= 100
    assert few["uncertain_rule"].startswith("at least 20% of 25 models")
    # A labelled cell is certain; the uncertain flag is the agreement rule.
    assert agreement[_row_of(ds, made["positives"][0])] == 100
    eligible = arrays["calls"] >= 0
    np.testing.assert_array_equal(
        arrays["uncertain"][eligible],
        learner.uncertain(agreement[eligible] / 100.0) & (agreement[eligible] < 100))


def test_a_persons_examples_outweigh_the_wands_and_only_theirs_calibrate():
    """The wand calls cells from 0 to 2 positive; a person says the ones at
    0.5-1 are not. Weighed equally the boundary sits between; with the
    wand's at half weight it moves towards the person's."""
    from plexora.plugins.gating.server.classifier import learner

    rng = np.random.default_rng(2)

    def examples(n_wand, n_person):
        wand = np.concatenate([rng.uniform(-2, 0, n_wand), rng.uniform(0, 2, n_wand)])
        x = np.concatenate([wand, rng.uniform(0.5, 1.0, n_person)])[:, None]
        return (x.astype(np.float32),
                np.concatenate([np.repeat([0, 1], n_wand), np.zeros(n_person, int)]))

    # Few enough that the model is the logistic one.
    x, y = examples(15, 6)
    grid = np.linspace(-2, 2, 401, dtype=np.float32)[:, None]

    def boundary(weights):
        fitted = learner.fit(x, y, weights=weights, n_given=6)
        assert fitted.kind == "logistic_regression"
        score, _ = fitted.scores(grid)
        return float(grid[np.argmax(score >= 0.5), 0])

    assert boundary(np.concatenate([np.full(30, 0.5), np.ones(6)])) > boundary(None) + 0.05
    # 72 examples, but only 12 a person's: a score, not a probability.
    x, y = examples(30, 12)
    assert not learner.fit(x, y, n_given=12).calibrated
    assert learner.fit(x, y).calibrated


def test_review_offers_uncertain_unlabelled_cells_and_drops_the_ones_labelled(ds, made):
    assert panel.review_cells(ds, "CD8")["cells"] == []
    _label(ds, made, n_pos=3, n_neg=3)
    # Borderline cells labelled both ways: the resampled models part over them.
    border = made["borderline"]
    for cell in border[:2]:
        panel.annotate(ds, "CD8", cell, "positive")
    for cell in border[2:4]:
        panel.annotate(ds, "CD8", cell, "negative")
    runs.run(ds, "CD8")
    _record, arrays = calls.run_arrays(ds, "CD8")
    unsure = {int(made["cells"][i]["id"]) for i in np.flatnonzero(arrays["uncertain"])}
    assert unsure, "three examples a side leave cells uncertain"
    first = panel.review_cells(ds, "CD8", count=4)
    offered = [c["cell_id"] for c in first["cells"]]
    assert 0 < len(offered) <= 4 and set(offered) <= unsure
    assert first["n_uncertain"] == len(unsure)
    labelled = set(made["positives"][:3]) | set(made["negatives"][:3]) | set(border[:4])
    assert not set(offered) & labelled
    assert all(np.isfinite(c["x"]) and 0 <= c["agreement"] <= 100 for c in first["cells"])
    # The same run offers the same queue; a cell labelled since drops out.
    assert [c["cell_id"] for c in panel.review_cells(ds, "CD8", count=4)["cells"]] == offered
    panel.annotate(ds, "CD8", offered[0], "positive")
    again = panel.review_cells(ds, "CD8", count=4)
    assert offered[0] not in [c["cell_id"] for c in again["cells"]]
    assert again["n_unlabelled"] == first["n_unlabelled"] - 1


def test_the_uncertain_band_is_as_wide_as_the_examples_leave_it():
    """Two overlapping populations: fitted on six examples the resampled
    models disagree over a wide stretch; on sixty, over a narrow one around
    where the populations cross."""
    from plexora.plugins.gating.server.classifier import learner

    rng = np.random.default_rng(1)

    def examples(n):
        x = np.concatenate([rng.normal(0, 1, n), rng.normal(2, 1, n)])[:, None]
        return x.astype(np.float32), np.repeat([0, 1], n)

    grid = np.linspace(-3, 5, 801, dtype=np.float32)[:, None]
    widths = []
    for n in (3, 30):
        fitted = learner.fit(*examples(n))
        _score, agreement = fitted.scores(grid)
        unsure = grid[learner.uncertain(agreement), 0]
        widths.append(float(unsure.max() - unsure.min()) if unsure.size else 0.0)
        assert unsure.size == 0 or (unsure.min() > -3 and unsure.max() < 5)
    assert widths[0] > widths[1] > 0
