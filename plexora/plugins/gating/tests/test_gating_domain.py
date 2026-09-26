"""Gating's state, read and written through a handle set rather than a name.

What an agent uses (plexora/plugins/gating/capabilities.py). The claims: the
stored shape is the sidebar's own, a write conflicts instead of clobbering, the
counts are the counts the viewer colours, and none of it loads the project the
viewer has open.
"""

import pickle

import numpy as np
import pytest

from plexora.plugins.gating.server import model
from plexora.server.models import data_model
from tests.agent_fixtures import (BORDERLINE, NEGATIVE, POSITIVE, local_handles,
                                  make_synthetic_project)


@pytest.fixture
def ds(tmp_path):
    info = make_synthetic_project(tmp_path)
    return local_handles(info["name"])


def test_an_ungated_project_reads_as_default_ranges(ds):
    assert model.gate_rows(ds) == []
    assert model.revision(ds) == "0"
    gate = model.get_gate(ds, "CD8")
    assert gate["thresholded"] is False
    assert gate["low"] == gate["default_low"] and gate["high"] == gate["default_high"]
    assert model.active_gates(ds) == {}
    assert model.get_gate(ds, "not_a_marker") is None


def test_set_gate_writes_the_sidebars_own_rows(ds):
    before, after, revision = model.set_gate(ds, "CD8", 400.0, 1e9)
    assert before["thresholded"] is False
    assert after["low"] == 400.0 and after["thresholded"] is True
    assert revision != "0"
    rows = pickle.loads(model._store(ds.name).get_state())
    by_channel = {row["channel"]: row for row in rows}
    # Every marker has a row -- the sidebar seeds one per marker it shows --
    # and the untouched one still sits at its full range.
    assert set(by_channel) == {"DNA", "CD8"}
    assert by_channel["CD8"]["gate_start"] == 400.0
    assert set(by_channel["CD8"]) == {"channel", "gate_start", "gate_end", "gate_active"}
    assert model.active_gates(ds) == {"CD8": (400.0, 1e9)}
    # And the name-taking reader the routes use sees the same list.
    assert model.get_saved_gating_list(ds.name) == rows


def test_a_stale_revision_conflicts_instead_of_clobbering(ds):
    _, _, first = model.set_gate(ds, "CD8", 400.0, 1e9)
    model.set_gate(ds, "CD8", 500.0, 1e9, expected_revision=first)
    with pytest.raises(model.GateConflict) as caught:
        model.set_gate(ds, "CD8", 600.0, 1e9, expected_revision=first)
    assert caught.value.current_revision == model.revision(ds)
    assert model.get_gate(ds, "CD8")["low"] == 500.0


def test_set_gate_refuses_nonsense(ds):
    with pytest.raises(KeyError):
        model.set_gate(ds, "nope", 1, 2)
    with pytest.raises(ValueError):
        model.set_gate(ds, "CD8", 5, 5)
    assert model.gate_rows(ds) == []


def test_gated_summary_counts_what_the_viewer_colours(ds):
    threshold = (BORDERLINE * 1.2 + POSITIVE * 0.8) / 2
    summary = model.gated_summary(ds, "CD8", threshold, 1e9)
    values = np.asarray(ds.table.columns(["CD8"])["CD8"])
    assert summary["n_positive"] == int(((values > threshold) & (values < 1e9)).sum())
    assert summary["n_cells"] == len(values)
    # The same rows the range mask the viewer draws with selects.
    assert summary["n_positive"] == int(ds.table.range_mask({"CD8": (threshold, 1e9)}).sum())


def test_the_fit_and_the_adjustment_are_deterministic(ds):
    fit = model.fit_for(ds, "CD8")
    assert fit is not None and len(fit["means"]) == 3
    assert fit["fitted_in_log"] is True
    model.set_gate(ds, "CD8", fit["gate"], model.get_gate(ds, "CD8")["high"])
    up, reason = model.adjusted_threshold(ds, "CD8", "up", "medium")
    down, _ = model.adjusted_threshold(ds, "CD8", "down", "medium")
    assert down < fit["gate"] < up
    assert "up" in reason
    # Never past the population centres, however large the step.
    for _ in range(12):
        model.adjust_gate(ds, "CD8", "up", "large")
    assert model.get_gate(ds, "CD8")["low"] < np.expm1(fit["means"][-1])
    small, _ = model.adjusted_threshold(ds, "CD8", "up", "small")
    large, _ = model.adjusted_threshold(ds, "CD8", "up", "large")
    current = model.get_gate(ds, "CD8")["low"]
    assert current <= small <= large


def test_a_quantile_step_when_there_is_no_fit(ds, monkeypatch):
    monkeypatch.setattr(model, "fit_for", lambda ds, channel: None)
    model.set_gate(ds, "CD8", NEGATIVE * 2, 1e9)
    up, reason = model.adjusted_threshold(ds, "CD8", "up", "large")
    assert up > NEGATIVE * 2
    assert "quantile" in reason


def test_nothing_here_loads_the_viewers_project(ds):
    model.set_gate(ds, "CD8", 400.0, 1e9)
    model.gated_summary(ds, "CD8")
    model.all_gates(ds)
    assert data_model._loaded_source is None
