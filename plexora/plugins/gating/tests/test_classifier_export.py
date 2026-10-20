"""The classifier's files and agent tools once a gating session's layer sits
beside the threshold: the wide CSVs with and without session layers, the
overrides audit CSV, annotations schema 2 (origins kept, never laundered;
version 1 still read), the gates export's classifier columns, and every
consumer reporting a classifier-decided marker by its calls."""

from __future__ import annotations

import csv
import io
import json

import numpy as np
import pytest

from plexora.plugins.gating.server import model
from plexora.plugins.gating.server.classifier import calls, export, panel, runs, store
from tests.agent_fixtures import local_handles, make_synthetic_project


@pytest.fixture
def made(tmp_path):
    return make_synthetic_project(tmp_path)


@pytest.fixture
def ds(made):
    return local_handles(made["name"])


def _gate(ds):
    values = np.asarray(ds.table.columns(["CD8"])["CD8"], dtype=np.float64)
    low = float(np.nanmedian(values))
    model.set_gate(ds, "CD8", low, float(np.nanmax(values)) + 1.0)
    return low


def _layer(ds, low, **margins):
    margins = margins or {"c_add": 0.4, "c_clean": 0.4}
    return runs.session_run(ds, "CD8", low=low, session_id="gs_x", **margins)["record"]


def _csv(ds, kind, **options):
    markers, chunks = export.stream_csv(ds, kind, **options)
    rows = list(csv.reader(io.StringIO("".join(chunks))))
    return markers, rows


def _label(ds, made, origin="click", n=4):
    for cell in made["positives"][:n]:
        panel.annotate(ds, "CD8", cell, "positive", origin=origin)
    for cell in made["negatives"][:n]:
        panel.annotate(ds, "CD8", cell, "negative", origin=origin)


# -- the wide CSVs --------------------------------------------------------------------


def test_a_session_layer_is_in_the_csvs_only_when_asked(ds, made):
    _layer(ds, _gate(ds))
    assert _csv(ds, "binary")[0] == []
    markers, rows = _csv(ds, "binary", include_session_runs=True)
    assert markers == ["CD8"] and rows[0] == ["image_id", "cell_id", "CD8"]
    _record, arrays = calls.session_arrays(ds, "CD8")
    from plexora.plugins.gating.server.classifier.geometry import cells

    ids = cells(ds).ids
    expected = {int(i): ("" if c < 0 else str(int(c))) for i, c in zip(ids, arrays["calls"])}
    assert {int(r[1]): r[2] for r in rows[1:]} == expected
    picked = export.plan(ds, "scores", include_session_runs=True)
    assert export.columns_of(picked)["CD8"]["which"] == "session_run"
    assert export.columns_of(picked)["CD8"]["active"] == "threshold"


def test_the_deciding_run_wins_over_a_session_layer(ds, made):
    _layer(ds, _gate(ds))
    decided = runs.run(ds, "CD8", auto_pick=True)
    picked = export.plan(ds, "binary", include_session_runs=True)
    assert picked["CD8"]["which"] == "run"
    assert picked["CD8"]["record"]["run_id"] == decided["run_id"]


def test_the_overrides_csv_marks_where_a_layer_parts_ways_with_its_gate(ds, made):
    low = _gate(ds)
    record = _layer(ds, low)
    markers, rows = _csv(ds, "overrides")
    assert markers == ["CD8"] and rows[0] == ["image_id", "cell_id", "CD8"]
    assert len(rows) == 1 + len(made["cells"])
    _held, arrays = calls.session_arrays(ds, "CD8")
    values = [r[2] for r in rows[1:]]
    added = int((arrays["calls"] == 1).__and__(arrays["gate_calls"] == 0).sum())
    removed = int((arrays["calls"] == 0).__and__(arrays["gate_calls"] == 1).sum())
    assert values.count("1") == added and values.count("-1") == removed
    assert added + removed > 0, "the margins were set so the layer overrides some cells"
    compared = calls.compare(ds, "CD8")
    assert (compared["n_added"], compared["n_removed"]) == (added, removed)
    assert set(values) <= {"1", "-1", "0", ""}
    assert record["gate_used"][0] == low


def test_a_run_made_at_no_gate_has_no_overrides_column(ds, made):
    _gate(ds)
    runs.run(ds, "CD8", auto_pick=True)
    assert export.plan(ds, "overrides") == {}
    assert _csv(ds, "overrides")[0] == []
    # Its layer at a gate is used instead once there is one.
    _layer(ds, model.get_gate(ds, "CD8")["low"])
    assert export.plan(ds, "overrides")["CD8"]["which"] == "session_run"


def test_override_codes():
    out = export.override_codes(np.array([1, 0, 1, 0, -1, 1], np.int8),
                                np.array([0, 1, 1, 0, 1, -1], np.int8))
    assert out.tolist() == [1, -1, 0, 0, -2, -2]


# -- annotations ------------------------------------------------------------------------


def test_annotations_v2_round_trip_keeps_every_origin(ds, made):
    _label(ds, made, origin="click", n=3)
    panel.annotate(ds, "CD8", made["positives"][5], "positive", origin="agent")
    runs.run(ds, "CD8", auto_pick=True)
    before = store.origins(ds.name, "CD8")
    assert {"click", "agent", "auto"} <= set(before.values())
    document = json.loads(json.dumps(export.annotations_document(ds)))
    assert document["schema_version"] == 2
    entry = document["markers"]["CD8"]
    by_cell = {e["cell_id"]: e for e in entry["examples"]}
    assert {by_cell[c]["origin"] for c in before} == set(before.values())
    assert entry["runs"]["active"] == "classifier"
    assert entry["runs"]["run"]["call_rule"] == "score_0.5"
    assert entry["runs"]["run"]["versions"]["rules"]
    assert entry["runs"]["session_run"] is None

    store.clear_examples(ds.name)
    report = export.preview(ds, document)
    assert report["origins"]["CD8"]["agent"] == 1
    export.apply(ds, report["token"], "replace")
    after = store.origins(ds.name, "CD8")
    assert set(after) == set(before)
    for cell, origin in before.items():
        # The wand's and an agent's labels come back as theirs; a click made
        # elsewhere is an import.
        assert after[cell] == (origin if origin in ("auto", "agent") else "import")


def test_an_origin_written_by_hand_cannot_promote_a_label(ds, made):
    _label(ds, made, n=3)
    document = export.annotations_document(ds)
    for example in document["markers"]["CD8"]["examples"]:
        example["origin"] = "person"
    store.clear_examples(ds.name)
    export.apply(ds, export.preview(ds, document)["token"], "replace")
    assert set(store.origins(ds.name, "CD8").values()) == {"import"}


def test_a_version_1_file_still_imports(ds, made):
    document = {"format": export.FORMAT, "schema_version": 1, "markers": {
        "CD8": {"positive": made["positives"][:3], "negative": made["negatives"][:3]}}}
    report = export.preview(ds, document)
    assert report["schema_version"] == 1 and report["markers"]["CD8"]["valid_positive"] == 3
    result = export.apply(ds, report["token"], "merge")
    assert result["counts"]["CD8"] == {"positive": 3, "negative": 3}
    assert set(store.origins(ds.name, "CD8").values()) == {"import"}


def test_a_marker_listed_for_its_runs_alone_never_empties_its_examples(ds, made):
    low = _gate(ds)
    _layer(ds, low)
    document = export.annotations_document(ds)
    # A session's layer keeps no examples: the marker is there for its runs.
    entry = document["markers"]["CD8"]
    assert entry["positive"] == [] and entry["runs"]["session_run"]["session_id"] == "gs_x"
    assert entry["runs"]["session_run"]["compare"]["n_called"] > 0
    _label(ds, made, n=3)
    with pytest.raises(export.ClassifierRefused, match="no examples"):
        export.preview(ds, document)
    assert store.counts(ds.name)["CD8"] == (3, 3)


def test_a_newer_file_is_refused(ds):
    with pytest.raises(export.ClassifierRefused, match="newer"):
        export.preview(ds, {"format": export.FORMAT, "schema_version": 99,
                            "markers": {"CD8": {"positive": [1]}}})


# -- consumers ------------------------------------------------------------------------


def test_gated_summary_counts_what_decides_the_marker(ds, made):
    low = _gate(ds)
    threshold = model.gated_summary(ds, "CD8")
    assert threshold["source"] == "threshold" and "threshold" not in threshold
    _layer(ds, low)
    # A layer does not decide: the summary is still the gate's.
    assert model.gated_summary(ds, "CD8")["n_positive"] == threshold["n_positive"]
    runs.adopt_session(ds, "CD8")
    decided = model.gated_summary(ds, "CD8")
    marker_calls, source = calls.marker_calls(ds, "CD8")
    assert source == "classifier" and decided["source"] == "classifier"
    assert decided["n_positive"] == int((marker_calls == 1).sum())
    assert decided["threshold"]["n_positive"] == threshold["n_positive"]
    assert decided["low"] == threshold["low"]
    # A range that is asked about is the range's.
    assert model.gated_summary(ds, "CD8", low)["source"] == "threshold"


def test_the_gates_export_names_the_source_and_the_run_beside_it(ds, made):
    from plexora.plugins.gating.server.autogate import report

    low = _gate(ds)
    record = _layer(ds, low)

    class Session:
        def data(self, project):
            return ds

    class Call:
        session = Session()

    rows = {r["channel"]: r for r in report._gate_rows(Call(), ds.name)}
    row = rows["CD8"]
    compared = calls.compare(ds, "CD8")
    assert row["source"] == "threshold" and row["classifier_run"] == record["run_id"]
    assert row["classifier_origin"] == "session" and row["classifier_session"] == "gs_x"
    assert row["call_rule"] == "gate_unless_confident"
    assert (row["n_added"], row["n_removed"]) == (compared["n_added"], compared["n_removed"])
    assert row["classifier_stale"] == ""
    assert rows["DNA"]["source"] == "threshold" and rows["DNA"]["classifier_run"] == ""
    long = report.provenance_long(Call(), [ds.name])
    cd8 = next(r for r in long if r["marker"] == "CD8")
    assert cd8["source"] == "threshold" and cd8["classifier_run"] == record["run_id"]
    runs.adopt_session(ds, "CD8")
    row = {r["channel"]: r for r in report._gate_rows(Call(), ds.name)}["CD8"]
    assert row["source"] == "classifier" and row["classifier_run"] == record["run_id"]


# -- the agent's tools ----------------------------------------------------------------


@pytest.fixture
def session(made):
    from plexora.agent import AgentSession, registry

    registry.discover(["gating"])
    return AgentSession()


def _ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:2000]
    return result["result"]


def test_the_export_tool_writes_overrides_and_session_layers(session, ds, made):
    from plexora.agent import invoke

    _layer(ds, _gate(ds))
    out = _ok(invoke(session, "classifier_export", {"project": made["name"]}))
    assert set(out["written"]) == {"binary", "scores", "overrides", "annotations"}
    assert out["columns"]["binary"]["CD8"]["active"] == "threshold"
    with open(out["written"]["overrides"], encoding="utf-8") as handle:
        assert handle.readline().strip() == "image_id,cell_id,CD8"
    only = _ok(invoke(session, "classifier_export", {
        "project": made["name"], "kinds": ["binary"], "include_session_runs": False}))
    assert only["written"] == {} and "binary" in only["skipped"]


def test_a_run_at_a_gate_is_a_layer_and_adopt_and_discard_take_it_on(session, ds, made):
    from plexora.agent import invoke, jobs

    low = _gate(ds)
    submitted = _ok(invoke(session, "classifier_run", {
        "project": made["name"], "marker": "CD8", "gate": low, "pixels": False}))
    jobs.drain(120)
    job = jobs.store().get(submitted["job_id"])
    assert job["status"] == "done", job.get("error")
    result = job["result"]
    assert result["slot"] == "session_run" and result["decides"] == "threshold"
    assert result["run"]["origin"] == "agent" and result["run"]["gate_used"][0] == low
    assert result["receipt"]["undo_hint"]["arguments"]["discard"] is True
    layer = store.session_run_record(ds.name, "CD8")
    assert layer["run_id"] == result["run"]["run_id"] and layer["gate_source"] == "agent"
    assert store.active(ds.name, "CD8") == "threshold"

    queue = _ok(invoke(session, "classifier_review", {"project": made["name"],
                                                      "marker": "CD8", "count": 3}))
    assert len(queue["cells"]) <= 3
    if queue["cells"]:
        # Which run's cells depends on panel.review_cells; what the tool
        # tells the agent does not.
        assert "render_cell_gallery" in queue["next"] and "Only the user" in queue["next"]

    adopted = _ok(invoke(session, "classifier_adopt", {"project": made["name"],
                                                       "marker": "CD8"}))
    assert adopted["active"] == "classifier"
    assert adopted["receipt"]["undo_hint"]["tool"] == "classifier_revert"
    assert calls.marker_calls(ds, "CD8")[1] == "classifier"

    reverted = _ok(invoke(session, "classifier_revert", {"project": made["name"],
                                                         "marker": "CD8"}))
    assert reverted["reverted"] and not reverted["discarded"]
    assert store.session_run_record(ds.name, "CD8") is not None
    discarded = _ok(invoke(session, "classifier_revert", {
        "project": made["name"], "marker": "CD8", "discard": True}))
    assert discarded["discarded"] and store.session_run_record(ds.name, "CD8") is None
    refused = invoke(session, "classifier_adopt", {"project": made["name"], "marker": "CD8"})
    assert not refused["ok"] and refused["error"]["code"] == "precondition_missing"
