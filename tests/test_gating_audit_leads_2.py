"""Regression tests for the second batch of gating leads of the Nov 2026 audit
(docs/internal/AUDIT_2026-11-14.md §7), one block per lead id."""

from __future__ import annotations

import os
import pathlib
import threading
import types

import numpy as np
import pytest

pytestmark = pytest.mark.pro


# -- pg-gating-rest-7: a run's arrays are never deleted under it ------------------------


def _session_result(run_id, n=50):
    return {"record": {"status": "ready", "run_id": run_id},
            "arrays": {"calls": np.ones(n, np.int8), "scores": np.zeros(n, np.float32),
                       "uncertain": np.zeros(n, bool)}}


def _every_named_run_has_arrays(store, project):
    for marker, entry in store.read_state(project)["markers"].items():
        for run_id in store.kept_run_ids(entry or {}):
            assert store.read_arrays(project, marker, run_id) is not None, (marker, run_id)


def test_a_stale_keep_never_deletes_a_run_the_state_names(tmp_path):
    from plexora.plugins.gating.server.classifier import runs, store
    from tests.agent_fixtures import make_synthetic_project

    make_synthetic_project(tmp_path)
    ds = types.SimpleNamespace(name="synth")
    runs.store_session_result(ds, "CD8", _session_result("aaaaaaaaaaaa"))
    # A keep computed before that run was recorded (another edit's).
    store.remove_runs("synth", "CD8", keep=set())
    _every_named_run_has_arrays(store, "synth")


def test_an_edit_between_writing_arrays_and_recording_the_run_leaves_them(tmp_path,
                                                                           monkeypatch):
    from plexora.plugins.gating.server.classifier import runs, store
    from tests.agent_fixtures import make_synthetic_project

    make_synthetic_project(tmp_path)
    ds = types.SimpleNamespace(name="synth")
    real = store.write_arrays
    others = []

    def write_then_race(*args, **kwargs):
        path = real(*args, **kwargs)
        # Another thread drops the marker's session layer meanwhile.
        other = threading.Thread(target=runs.drop_session_run, args=(ds, "CD8"))
        other.start()
        other.join(0.5)
        others.append(other)
        return path

    monkeypatch.setattr(store, "write_arrays", write_then_race)
    runs.store_session_result(ds, "CD8", _session_result("bbbbbbbbbbbb"))
    for other in others:
        other.join(10)
    _every_named_run_has_arrays(store, "synth")


# -- pg-autogate-a-10: the panel context's save and load wait out Windows refusals ------


def test_panel_context_save_and_load_retry_a_sharing_violation(tmp_path, monkeypatch):
    from plexora.plugins.gating.server.autogate import context

    monkeypatch.setattr(context, "root", lambda: tmp_path)
    real_replace, real_read = os.replace, pathlib.Path.read_text
    refused = {"replace": 1, "read": 1}

    def flaky_replace(src, dst):
        if refused["replace"]:
            refused["replace"] -= 1
            raise PermissionError("in use")
        return real_replace(src, dst)

    def flaky_read(self, *args, **kwargs):
        if self.name == "panel_context.json" and refused["read"]:
            refused["read"] -= 1
            raise PermissionError("in use")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)
    context.save({"panel_hash": "abc", "markers": ["CD3"]})
    monkeypatch.setattr(pathlib.Path, "read_text", flaky_read)
    assert context.load("abc") == {"panel_hash": "abc", "markers": ["CD3"]}


# -- pg-autogate-a-6: summary_of counts each review state once ---------------------------


def test_summary_counts_not_binary_and_insufficient_once():
    from plexora.plugins.gating.server.autogate import engine

    summary = engine.summary_of({"units": {
        "a": {"state": "not_binary"}, "b": {"state": "insufficient_information"},
        "c": {"state": "manual_review_recommended"}}})
    assert summary["review"] == 3


# -- pg-autogate-a-7: replan off never holds a unit for a replan packet -----------------


def test_cannot_tell_with_replan_off_is_not_held():
    from plexora.plugins.gating.server.autogate import replan

    engine = types.SimpleNamespace(options={"replan": False}, record={},
                                   log=lambda **_k: None)
    unit = {"project": "p", "marker": "CD8", "state": "qc_confirm",
            "replan_from": "awaiting_regression", "lineage_check": True,
            "lineage_contamination": {"partner": "CD4", "at_top": 0.5, "prevalence": 0.1}}
    assert replan.after_whole_image(engine, unit, "cannot_tell") is True
    assert unit["state"] == "awaiting_regression"
    assert not replan.pending(engine.record)


# -- pg-autogate-a-9: the dataset spread leaves empty gates out -------------------------


def test_dataset_spread_leaves_empty_gates_out():
    from plexora.plugins.gating.server.autogate import report

    units = [{"marker": "CD8", "state": "accepted", "final": 1.0},
             {"marker": "CD8", "state": "manual_review_recommended", "final": 2.0,
              "needs_review": True},
             {"marker": "CD8", "state": "technically_failed", "final": 900.0},
             {"marker": "CD3", "state": "no_positive_population", "final": 50.0}]
    spread = report.dataset_spread(units, ["CD8", "CD3"])
    assert spread["CD8"]["max"] == 2.0 and spread["CD8"]["n_images"] == 2
    assert (spread["CD8"]["n_empty"], spread["CD8"]["n_review"]) == (1, 1)
    assert spread["CD3"]["n_images"] == 0 and spread["CD3"]["max"] is None


# -- pg-autogate-b-2: other anchors' reads never use up the scale image's cells ---------


def _pooled_state():
    points = [{"low": 0.0, "n_positive": 10, "id": "a"},
              {"low": 1.0, "n_positive": 4, "id": "b"},
              {"low": 2.0, "n_positive": 0, "id": "c"}]
    other = [{"value": 0.5, "image": "img2", "label": "own_stain"} for _ in range(6)]
    own = [{"value": 0.5, "label": "own_stain"}, {"value": 1.5, "label": "own_stain"}]
    return {"points": points, "window": [0, 2],
            "observations": [{"cells": other, "valid": True}, {"cells": own, "valid": True}]}


def test_pooled_reads_do_not_count_as_shown_scale_cells():
    from plexora.plugins.gating.server.autogate.hypotheses import controller

    state = _pooled_state()
    assert controller.shown(state) == [1, 1]
    tail = controller.tail(state, {"low": 0.0, "positives": 10})
    assert tail["available"] == 8 and tail["read"] == 8


# -- pg-autogate-b-3: a version change keeps an in-progress unit's reads on record ------


def test_a_stale_version_in_progress_state_is_archived():
    from plexora.plugins.gating.server.autogate import hypotheses

    class Stop(Exception):
        pass

    def lattice_for(_unit):
        raise Stop

    engine = types.SimpleNamespace(lattice_for=lattice_for, log=lambda **_k: None)
    old = {"version": "0", "status": None, "observations": [{"cells": [{"value": 1}]}]}
    unit = {"project": "p", "marker": "CD8", "hypotheses": old}
    with pytest.raises(Stop):
        hypotheses.start(engine, unit)
    assert unit["hypotheses_history"][-1]["observations"] == old["observations"]


# -- pg-autogate-b-4: the numeric checks count the gate as written, high bound too ------


def test_numeric_checks_respect_the_high_bound(tmp_path):
    from plexora import api
    from plexora.plugins.gating.server.autogate import profile as profmod
    from plexora.plugins.gating.server.autogate import regression
    from tests.agent_fixtures import make_synthetic_project

    make_synthetic_project(tmp_path)
    ds = api.project_data("synth")
    col = profmod.column(ds, "CD8")
    s = col.sorted32
    low, high = float(s[len(s) // 4]), float(s[3 * len(s) // 4])
    result = regression.numeric_checks(ds, "CD8", low, low, high=high)
    assert result["n_positive"] == col.n_positive(low, high) < col.n_positive(low)


# -- pg-autogate-b-5: each tile's noise ceiling is read above the floor spike -----------


def test_region_ceiling_is_read_above_the_floor_spike(monkeypatch):
    from plexora.plugins.gating.server.autogate import profile as profmod
    from plexora.plugins.gating.server.autogate import scoring

    rng = np.random.default_rng(0)
    n = 9 * 2000
    raw = np.concatenate([np.zeros(int(n * 0.1)), np.exp(rng.normal(4, 0.4, int(n * 0.75))),
                          np.exp(rng.normal(6, 0.4, n - int(n * 0.1) - int(n * 0.75)))])
    rng.shuffle(raw)
    col = profmod.Column("m", raw, False)
    assert col.floor_n > 0
    whole = scoring.background(col.body)["ceiling"]
    x = col.to_fit(raw)
    finite = np.isfinite(x)
    monkeypatch.setattr(scoring, "_tiles", lambda _ds, ok, k: np.arange(raw.size) % (k * k))
    cellmod = types.SimpleNamespace(eligible=lambda _ds, _m: np.ones(raw.size, bool))
    region = scoring._region(None, "m", x, finite, [], [], 0.0, 8.0, 0.02, col, cellmod,
                             profmod)
    assert region is not None and abs(region["median"] - whole) < 0.5


# -- pg-autogate-b-6: a carried review gate never closes a member that left the group ---


def test_carried_review_skips_members_no_longer_pending(monkeypatch):
    from plexora.plugins.gating.server.autogate import groups

    closed = []
    monkeypatch.setattr(groups, "_carried", lambda *a: 1.0)
    units = {"s::CD8": {"project": "s", "marker": "CD8", "state": "manual_review_recommended",
                        "final": 2.0, "reason": "unsettled"},
             "m1::CD8": {"project": "m1", "marker": "CD8", "state": "group_pending"},
             "m2::CD8": {"project": "m2", "marker": "CD8", "state": "manual_review_recommended",
                         "user_edited": True}}
    engine = types.SimpleNamespace(
        record={"units": units},
        close=lambda unit, state, reason, **kw: closed.append(unit["project"]))
    group = {"id": "g1", "marker": "CD8", "scale": "s", "members": ["s", "m1", "m2"]}
    groups.carry(engine, group)
    assert closed == ["m1"]
