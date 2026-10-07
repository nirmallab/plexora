"""DNA retention across cycles: until which cycle a cell keeps its nucleus,
the labels with no nucleus, and what both do to the cells' calls."""

import json

import numpy as np
import polars as pl
import pytest

from plexora.agent import AgentSession, invoke, jobs, registry


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])
    yield
    from plexora.agent import render
    from plexora.server.utils import source_image

    render.close_masks()
    source_image.close_readers()


def ok(answer):
    assert answer["ok"], json.dumps(answer.get("error"), default=str)[:3000]
    return answer["result"]


def test_retention_is_monotone_and_says_until_which_cycle():
    from plexora.plugins.qc.server import dna_retention

    rng = np.random.default_rng(0)
    n, k = 1000, 10
    J = rng.uniform(0.8, 1.2, size=(n, k))
    lose3, lose7 = np.arange(0, 30), np.arange(30, 100)
    J[lose3, 2:] = 0.05                    # gone from cycle 3
    J[lose7, 6:] = 0.05                    # gone from cycle 7
    J[0, 5] = 1.0                          # back in cycle 6: still lost at 3
    J[100:110, 0] = 0.02                   # never had a nucleus
    out = dna_retention.derive_retention(J, 0, list(range(1, k + 1)))
    last = out["last_good_cycle"]
    assert (last[lose3] == 2).all() and (last[lose7] == 6).all()
    assert (last[100:110] == 0).all() and not out["has_nucleus"][100:110].any()
    assert (last[110:] == k).all()
    nucleated = n - 10
    cycles = {c["index"]: c for c in out["cycles"]}
    assert cycles[3]["n_lost"] == 30 and cycles[7]["n_lost"] == 70
    assert cycles[2]["cumulative_retained"] == 1.0
    assert cycles[3]["cumulative_retained"] == pytest.approx((nucleated - 30) / nucleated, 1e-3)
    through = [c["cumulative_retained"] for c in out["cycles"]]
    assert all(a >= b for a, b in zip(through, through[1:]))
    # 97 % keep it through cycle 6, 90 % through 7: reliable through cycle 6.
    assert out["reliable_through_cycle"] == 6
    line = dna_retention.digest_line({"cycles": out["cycles"],
                                      "reliable_through_cycle": out["reliable_through_cycle"],
                                      "n_cells": n, "n_no_nucleus": 10})
    assert line.startswith("reliable through cycle 6 of 10") and "1.0% of labels" in line


def test_no_nucleus_flags_only_the_dim_tail():
    from plexora.plugins.qc.server import dna_retention

    rng = np.random.default_rng(1)
    glass = rng.normal(60.0, 4.0, size=5000)
    lo, hi = 60.0, 3060.0
    at = dna_retention.no_nucleus_at(glass, lo, hi)
    assert at == pytest.approx(0.1)        # a quiet glass: the presence floor
    noisy = rng.normal(60.0, 400.0, size=5000)
    assert dna_retention.no_nucleus_at(noisy, lo, hi) > 0.3
    cells = np.concatenate([rng.uniform(0.6, 1.2, 500), rng.uniform(0.0, 0.05, 20)])
    flagged = cells < at
    assert flagged[500:].all() and not flagged[:500].any()


@pytest.fixture
def dropout(tmp_path):
    from tests.qc_fixtures import make_qc_project

    return make_qc_project(tmp_path, artifacts=("cycle_dropout",))


def _lost_ids(info):
    return {cid for cid, why in info["truth"]["cells"].items() if "cycle_loss" in why}


def _activate(session):
    """An active result for the cells to be derived into (a session's, or one
    drawn by hand)."""
    from plexora.plugins.qc.server import results

    with results.lock("qcsynth"):
        document = results.load("qcsynth")
        results.ensure_active(document, "qcsynth")
        results.save("qcsynth", document)


def test_a_cycle_lost_flags_its_markers_in_the_cells_that_lost_it(dropout):
    from plexora.plugins.qc.server import exclusions, results

    lost = _lost_ids(dropout)
    assert lost
    session = AgentSession()
    _activate(session)
    ran = ok(invoke(session, "run_dna_retention", {"project": "qcsynth"}))
    jobs.drain(180)
    status = ok(invoke(session, "get_dna_retention", {"project": "qcsynth"}))["dna_retention"]
    summary = status["summary"]
    assert summary is not None and not status["stale"], (ran, status)
    assert summary["reference"] == "DNA_1" and [c["index"] for c in summary["cycles"]] == [1, 2]
    assert status["digest"].startswith("reliable through cycle")
    from plexora.plugins.qc.server import dna_retention

    _summary, frame = dna_retention.calls("qcsynth")
    by_id = dict(zip(frame["cell_id"].to_list(), frame["last_good_cycle"].to_list()))
    assert all(by_id[cid] == 1 for cid in lost)
    kept = [cid for cid in by_id if cid not in lost]
    assert np.mean([by_id[cid] == 2 for cid in kept]) > 0.98
    cells = results.cells("qcsynth")
    rows = {r["cell_id"]: r for r in cells.iter_rows(named=True)}
    for cid in lost:
        assert {"DNA_2", "CD20"} <= set(rows[cid]["unreliable_markers"]), rows[cid]
        assert any(f.endswith("|dna_loss|exclude") for f in rows[cid]["marker_flags"])
        assert rows[cid]["pass"]           # a marker flag never fails the cell
    flagged = {cid for cid, row in rows.items() if "CD20" in (row["unreliable_markers"] or [])}
    assert flagged >= lost and len(flagged - lost) <= 0.02 * len(rows)
    ds = session.data("qcsynth")
    by_marker = exclusions.summary(ds, "strict")["by_marker"]
    assert by_marker.get("CD20") == len(flagged)
    evidence = [e for e in results.active(results.load("qcsynth"))["cells"]["marker_evidence"]
                if e.get("test") == "dna_retention"]
    assert {e["marker"] for e in evidence} >= {"DNA_2", "CD20"}


def test_no_nucleus_warns_and_excludes_under_strict(dropout):
    from plexora.plugins.qc.server import strictness
    from plexora.plugins.qc.server.cells import calls

    session = AgentSession()
    ds = session.data("qcsynth")
    ids = [c["id"] for c in dropout["cells"]]
    gone = set(ids[:5])
    frame = pl.DataFrame({"cell_id": ids, "n_px": [100.0] * len(ids),
                          "j_ref": [0.02 if i in gone else 1.0 for i in ids],
                          "has_nucleus": [i not in gone for i in ids],
                          "no_nucleus": [i in gone for i in ids],
                          "last_good_cycle": [0 if i in gone else 2 for i in ids]})
    summary = {"version": "1", "fingerprint": "fp", "reference": "DNA_1",
               "params": {"present_floor": 0.1}, "no_nucleus_at": 0.1,
               "cycles": [{"index": 1, "channel": "DNA_1", "markers": ["DNA_1", "CD3"]},
                          {"index": 2, "channel": "DNA_2", "markers": ["DNA_2", "CD20"]}]}
    result = {"result_id": "qr_test", "candidates": {}, "cycles": []}
    for preset, status in (("standard", "warn"), ("strict", "exclude")):
        cells, _pairs, out = calls.derive(ds, result, strictness.thresholds(preset),
                                          dna=(summary, frame))
        rows = {r["cell_id"]: r for r in cells.iter_rows(named=True)}
        assert all("no_nucleus" in rows[i]["reasons"] for i in gone)
        assert out["n_no_nucleus"] == len(gone)
        if status == "warn":
            assert out["warn_by_reason"]["no_nucleus"] == len(gone) and out["n_fail"] == 0
        else:
            assert out["by_reason"]["no_nucleus"] == len(gone)
            assert all(not rows[i]["pass"] for i in gone)
        # No nucleus is not "lost a cycle": no dna_loss on those cells.
        assert not any("dna_loss" in "".join(rows[i]["marker_flags"]) for i in gone)


def test_the_state_and_the_report_carry_the_digest(dropout):
    import plexora
    from plexora.agent.audit import AuditLog
    from plexora.agent.policy import Policy
    from plexora.agent.receipts import operation_id
    from plexora.agent.registry import Call
    from plexora.plugins.qc.server import report, results

    session = AgentSession()
    _activate(session)
    ok(invoke(session, "run_dna_retention", {"project": "qcsynth"}))
    jobs.drain(180)
    state = plexora.app.test_client().get("/plugins/qc/state?datasource=qcsynth").get_json()
    digest = state["dna_retention"]["digest"]
    assert digest.startswith("reliable through cycle")
    call = Call(capability=registry.get("qc.get_results"), session=session, policy=Policy(),
                operation_id=operation_id(), audit=AuditLog(), arguments={})
    built = report.build(call, "qcsynth", results.active(results.load("qcsynth")))
    page = report.to_html(call, built)
    assert "DNA retention by cycle" in page and digest.replace(">", "&gt;") in page
    assert "Cells outside the tissue (annotated, kept)" in page
    assert "<th>audit verdict</th>" in page and "<th>cells noted</th>" in page
    assert "Markers flagged in cells" in page and "<th>test</th>" not in page
