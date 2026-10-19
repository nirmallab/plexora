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
    assert result == {"reverted": True, "active": "threshold"}
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

    pos, neg, info = autopick.pick(ds, "CD8")
    assert len(pos) == len(neg) == autopick.N_PER_CLASS
    assert set(pos) <= set(made["positives"]), "a picked positive is a positive cell"
    assert set(neg) <= set(made["negatives"]) | set(made["borderline"]) - set(made["positives"])
    assert not set(pos) & set(made["borderline"]) or info["how"] == "ranks"
    # Spread: the picks are not one corner of the grid.
    rows = {int((c["y"] // 64)) for c in made["cells"] if c["id"] in pos}
    assert len(rows) >= 4
    # The same project picks the same cells again.
    assert autopick.pick(ds, "CD8")[0] == pos


def test_a_wand_run_keeps_a_persons_labels_and_replaces_its_own(ds, made):
    border = made["borderline"][0]
    panel.annotate(ds, "CD8", border, "negative")
    record = runs.run(ds, "CD8", auto_pick=True)
    assert record["auto_pick"]["how"] in ("gate", "ranks")
    origins = store.origins(ds.name, "CD8")
    assert origins[border] == "click"
    assert sum(1 for o in origins.values() if o == "auto") == 20
    payload = panel.examples_payload(ds, "CD8")
    assert payload["n_auto"] == 20
    assert any(e["auto"] for e in payload["positive"])
    assert not next(e for e in payload["negative"] if e["cell_id"] == border)["auto"]
    # A second press replaces the wand's picks, never adds to them.
    runs.run(ds, "CD8", auto_pick=True)
    assert sum(1 for o in store.origins(ds.name, "CD8").values() if o == "auto") == 20
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
