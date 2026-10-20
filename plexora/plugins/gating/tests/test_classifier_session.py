"""A gating session's layer in the classifier's own terms (`runs.session_run`,
`rules`, `calls`): made at a gate, kept in a slot of its own, never deciding
the marker until adopted, and what the eraser, Discard and Reset do to it."""

from __future__ import annotations

import numpy as np
import pytest

from plexora.plugins.gating.server import model
from plexora.plugins.gating.server.classifier import (
    VIEW_ADDED, VIEW_REMOVED, ClassifierRefused, calls, panel, rules, runs, store)
from tests.agent_fixtures import local_handles, make_synthetic_project


@pytest.fixture
def made(tmp_path):
    return make_synthetic_project(tmp_path)


@pytest.fixture
def ds(made):
    return local_handles(made["name"])


def _gate(ds):
    values = np.asarray(ds.table.columns(["CD8"])["CD8"], dtype=np.float64)
    return float(np.nanmedian(values))


def _set(ds, low):
    values = np.asarray(ds.table.columns(["CD8"])["CD8"], dtype=np.float64)
    model.set_gate(ds, "CD8", low, float(np.nanmax(values)) + 1.0)


# -- the rule ------------------------------------------------------------------


def test_gate_unless_confident_takes_positives_away_more_readily_than_it_adds():
    scores = np.array([0.05, 0.25, 0.6, 0.97, 0.99, 0.5, np.nan], dtype=np.float32)
    gate = np.array([1, 1, 1, 0, 0, -1, 1], dtype=np.int8)
    out = rules.apply(scores, gate, c_add=0.02, c_clean=0.3)
    # 0.05 and 0.25 above the gate: removed; 0.97 below: not confident enough
    # to add at 0.02; 0.99: added; not called stays not called; no score: -1.
    assert out.tolist() == [0, 0, 1, 0, 1, -1, -1]
    assert rules.apply(scores, gate, c_add=None, c_clean=0.3).tolist() == [0, 0, 1, 0, 0, -1, -1]
    assert rules.apply(scores, gate, c_add=None, c_clean=None).tolist() == [1, 1, 1, 0, 0, -1, -1]
    assert rules.apply(scores, gate, "score_0.5").tolist() == [0, 0, 1, 1, 1, -1, -1]
    counts = rules.compare(out, gate, raw_calls=rules.apply(scores, gate, "score_0.5"))
    assert counts["n_added"] == 1 and counts["n_removed"] == 2
    assert counts["n_raw_added"] == 2 and counts["n_raw_removed"] == 2
    with pytest.raises(ValueError):
        rules.apply(scores, gate, "vote")


def test_an_override_can_be_held_to_the_resampled_models_agreeing():
    scores = np.array([0.1, 0.1], dtype=np.float32)
    gate = np.array([1, 1], dtype=np.int8)
    out = rules.apply(scores, gate, c_clean=0.3, agreement=np.array([0.95, 0.6]),
                      min_agreement=0.8)
    assert out.tolist() == [0, 1]


def test_signal_grades_from_the_separation_and_never_from_nothing():
    assert rules.grade(d=5.0) == "ok"
    assert rules.grade(d=1.2) == "low"
    assert rules.grade(d=0.5) == "low"      # no grade skips a marker
    # Nothing known: only cleaning, never adding.
    assert rules.grade() == "low"
    assert rules.ashman_d([0.0, 4.0], [1.0, 1.0]) == pytest.approx(4.0)


def test_the_defaults_are_the_benched_ones():
    """CLASSIFIER_RULE_2026-10-19: cleaning only, at 0.3."""
    assert rules.C_CLEAN == 0.3 and rules.C_ADD is None


# -- the session's slot ------------------------------------------------------------


def test_a_session_run_is_kept_beside_the_threshold_and_never_decides(ds, made):
    gate = _gate(ds)
    _set(ds, gate)
    result = runs.session_run(ds, "CD8", low=gate, session_id="gs_x")
    record = result["record"]
    assert record["status"] == "ready" and record["origin"] == "session"
    assert record["session_id"] == "gs_x" and record["gate_used"][0] == gate
    assert record["call_rule"] == "gate_unless_confident"
    assert set(record["versions"]) >= {"feature", "autopick", "learner", "rules"}
    # The threshold still decides.
    assert store.active(ds.name, "CD8") == "threshold"
    assert store.run_record(ds.name, "CD8") is None
    assert calls.marker_calls(ds, "CD8")[1] == "threshold"
    # Raw calls and gate calls are on record beside the layer's.
    held, arrays = calls.session_arrays(ds, "CD8")
    assert held["run_id"] == record["run_id"]
    assert arrays["raw_calls"] is not None and arrays["gate_calls"] is not None
    gate_calls = calls.gate_calls_for(ds, "CD8", (gate, None))
    assert np.array_equal(arrays["gate_calls"], gate_calls)
    # The layer is the gate's call except where it removed positives.
    differ = arrays["calls"] != gate_calls
    assert np.all((gate_calls[differ] == 1) & (arrays["calls"][differ] == 0))
    compared = calls.compare(ds, "CD8")
    assert compared["n_added"] == 0 and compared["stale"] is None
    band = record["band"]
    assert band is None or band["low"] < gate < band["high"]
    assert record["auto_pick"]["gate_from"] == "given"


def test_picks_at_a_given_gate_stay_out_of_the_band_beside_it(ds, made):
    from plexora.plugins.gating.server.classifier import autopick
    from plexora.plugins.gating.server.classifier.geometry import cells

    gate = _gate(ds)
    values = np.asarray(ds.table.columns(["CD8"])["CD8"], dtype=np.float64)
    lo, hi = float(np.nanquantile(values, 0.4)), float(np.nanquantile(values, 0.6))
    pos, neg, info = autopick.pick(ds, "CD8", gate=gate, exclude_range=(lo, hi))
    ids = cells(ds).ids
    picked = values[np.isin(ids, np.asarray(pos + neg, dtype=np.int64))]
    assert picked.size and not np.any((picked >= lo) & (picked <= hi))
    assert info["gate"] == gate and info["gate_from"] == "given"
    above = values[np.isin(ids, np.asarray(pos, dtype=np.int64))]
    assert np.all(above > gate)


def test_the_panel_draws_a_session_layer_with_its_disagreements(ds, made, monkeypatch):
    gate = _gate(ds)
    _set(ds, gate)
    runs.session_run(ds, "CD8", low=gate, session_id="gs_x", c_add=0.4, c_clean=0.4)
    codes, headers = calls.viewer_codes(ds, "CD8")
    assert headers["which"] == "session_run" and headers["state"] == "ready"
    _record, arrays = calls.session_arrays(ds, "CD8")
    added = (arrays["calls"] == 1) & (arrays["gate_calls"] == 0)
    removed = (arrays["calls"] == 0) & (arrays["gate_calls"] == 1)
    assert headers["added"] == int(added.sum()) and headers["removed"] == int(removed.sum())
    assert np.all(codes[added] == VIEW_ADDED) and np.all(codes[removed] == VIEW_REMOVED)
    raw_codes, raw_headers = calls.viewer_codes(ds, "CD8", view="raw")
    assert raw_headers["view"] == "raw"
    raw_added = (arrays["raw_calls"] == 1) & (arrays["gate_calls"] == 0)
    assert raw_headers["added"] == int(raw_added.sum())


def test_a_wand_press_keeps_the_sessions_layer_and_adopting_it_decides(ds, made):
    gate = _gate(ds)
    _set(ds, gate)
    session = runs.session_run(ds, "CD8", low=gate, session_id="gs_x")["record"]
    panel_run = runs.run(ds, "CD8", auto_pick=True)
    # Two runs, two files: the wand's did not delete the session's.
    assert store.session_run_record(ds.name, "CD8")["run_id"] == session["run_id"]
    assert store.read_arrays(ds.name, "CD8", session["run_id"]) is not None
    assert store.read_arrays(ds.name, "CD8", panel_run["run_id"]) is not None
    adopted = runs.adopt_session(ds, "CD8")
    assert adopted["active"] == "classifier" and adopted["run_id"] == session["run_id"]
    assert store.run_record(ds.name, "CD8")["origin"] == "session"
    assert calls.marker_calls(ds, "CD8")[1] == "classifier"
    # The panel's run is gone (replaced); the session's file stays.
    assert store.read_arrays(ds.name, "CD8", panel_run["run_id"]) is None
    # The eraser: Thresholding decides again, the layer is still drawn.
    erased = runs.revert(ds, "CD8")
    assert erased["reverted"] and not erased["discarded"]
    assert store.active(ds.name, "CD8") == "threshold"
    assert store.session_run_record(ds.name, "CD8") is not None
    assert store.read_arrays(ds.name, "CD8", session["run_id"]) is not None
    # Discard: gone.
    assert runs.drop_session_run(ds, "CD8")["discarded"]
    assert store.session_run_record(ds.name, "CD8") is None
    assert store.read_arrays(ds.name, "CD8", session["run_id"]) is None
    with pytest.raises(ClassifierRefused):
        runs.adopt_session(ds, "CD8")


def test_a_rolled_back_session_takes_its_layers_and_an_adoption_of_one(ds, made):
    gate = _gate(ds)
    _set(ds, gate)
    runs.session_run(ds, "CD8", low=gate, session_id="gs_a")
    runs.adopt_session(ds, "CD8")
    touched = runs.discard_session_runs(ds, "gs_a")
    assert touched == ["CD8"]
    assert store.active(ds.name, "CD8") == "threshold"
    assert store.session_run_record(ds.name, "CD8") is None
    assert runs.discard_session_runs(ds, "gs_other") == []


def test_reset_drops_session_layers_too(ds, made):
    gate = _gate(ds)
    _set(ds, gate)
    runs.session_run(ds, "CD8", low=gate, session_id="gs_a")
    result = runs.reset(ds)
    assert result["reverted"] == ["CD8"]
    assert store.session_run_record(ds.name, "CD8") is None


def test_a_layer_whose_gate_moved_is_stale(ds, made):
    gate = _gate(ds)
    _set(ds, gate)
    record = runs.session_run(ds, "CD8", low=gate, session_id="gs_a")["record"]
    assert runs.stale_reason(ds, record) is None
    _set(ds, gate * 1.5)
    assert runs.stale_reason(ds, store.session_run_record(ds.name, "CD8")) == \
        "the gate moved since this run"


def test_computing_a_layer_without_keeping_it_stores_nothing(ds, made):
    gate = _gate(ds)
    result = runs.session_run(ds, "CD8", low=gate, session_id="gs_a", keep=False)
    assert result["record"]["status"] == "ready" and result["record"]["stored"] is None
    assert store.session_run_record(ds.name, "CD8") is None
    runs.store_session_result(ds, "CD8", result)
    assert store.session_run_record(ds.name, "CD8")["stored"] == "project"


def test_a_marker_that_barely_separates_still_gets_a_layer(ds, made, monkeypatch):
    """p193's CD8a (D 0.67): the classifier at the gate found most of the weak
    positives the user gates; a skip would have hidden them."""
    from plexora.plugins.gating.server.classifier import signal

    monkeypatch.setattr(signal, "separation", lambda ds_, marker: 0.3)
    result = runs.session_run(ds, "CD8", low=_gate(ds), session_id="gs_a")
    assert result["record"]["status"] == "ready" and result["record"]["snr"]["grade"] == "low"
    assert result["arrays"] is not None


def test_low_signal_never_adds(ds, made, monkeypatch):
    from plexora.plugins.gating.server.classifier import signal

    monkeypatch.setattr(signal, "separation", lambda ds_, marker: 1.2)
    record = runs.session_run(ds, "CD8", low=_gate(ds), session_id="gs_a", c_add=0.45,
                              c_clean=0.3)["record"]
    assert record["snr"]["grade"] == "low" and record["c_add_used"] is None
    assert record["compare"]["n_added"] == 0


# -- whose examples weigh what -----------------------------------------------------


def test_only_a_persons_labels_weigh_one_and_count_toward_calibration(ds, made):
    for cell in made["positives"][:3]:
        panel.annotate(ds, "CD8", cell, "positive", origin="agent")
    for cell in made["negatives"][:3]:
        panel.annotate(ds, "CD8", cell, "negative")
    rows, _labels, _report = runs.example_rows(ds, "CD8")
    weights, n_given = runs.example_weights(ds, "CD8", rows)
    assert n_given == 3
    assert sorted(weights.tolist()) == [0.5, 0.5, 0.5, 1.0, 1.0, 1.0]
