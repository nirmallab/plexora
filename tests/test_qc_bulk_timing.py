"""Timing budgets for the QC session's deterministic bulk pass.

Two things are pinned here, both against the live run that first surfaced
them (`test_qc_live_run_fixes.py`'s docstring: a 45k x 53k px, 40-channel
image whose bulk job sat silent at "scanned TubbIII" for twenty minutes):

- the bulk pass itself (`plexora.plugins.qc.server.bulk.run`, started by
  `qc_session_start`) -- scanning, detectors, candidates, checks and cells --
  on a large-ish synthetic, each stage under a generous budget;
- `plexora.plugins.qc.server.candidates.merge` alone, at the candidate count
  a run with many small specks (a 40-channel aggregate detector) actually
  produced, which is a shape the bulk-pass fixture above does not reach
  (its scan grid and artifact mix raise only a few hundred candidates).

Budgets here are generous on purpose: this is a regression guard against
minutes-long blowups, not a performance benchmark, and must not be flaky on a
slower CI box.
"""

from __future__ import annotations

import time

import numpy as np
import pytest


def _timed(monkeypatch, module, name, timings, key=None):
    """Wrap `module.name` so every call's wall time accumulates in
    `timings[key]` -- the attribute is patched on `module` itself, so this
    reaches calls made as `module.name(...)` from anywhere, including code
    that imported `name` fresh (`from module import name`) inside a
    function body, since that re-reads the module's current attribute."""
    key = key or name
    original = getattr(module, name)

    def wrapper(*args, **kwargs):
        started = time.monotonic()
        try:
            return original(*args, **kwargs)
        finally:
            timings[key] = timings.get(key, 0.0) + (time.monotonic() - started)

    monkeypatch.setattr(module, name, wrapper)
    return timings


#: Generous per-stage ceilings (seconds) for the bulk-pass test below. Each is
#: roughly 3x what a 3072 px / 5-channel synthetic with several artifacts
#: takes on a laptop (load_or_run ~11s, checks_bulk ~11s dominate; detectors,
#: candidate build and the cell modules are all well under a second at this
#: scan-grid size) -- this guards against a regression to minutes, not
#: against day-to-day variance.
STAGE_BUDGETS_S = {"load_or_run": 35.0, "run_all": 15.0, "build": 10.0,
                   "checks_bulk": 35.0, "cell_bulk": 15.0}
TOTAL_BUDGET_S = 60.0


@pytest.mark.paid
def test_the_bulk_pass_stages_stay_within_a_generous_budget(tmp_path, monkeypatch):
    """A large-ish synthetic (qc_fixtures' fixed 5 channels, a 3072 px image,
    several artifacts so every stage has real work: detectors, the checks and
    the cell modules all have something to score) run once through
    `qc_session_start`'s bulk job. `jobs.drain` runs the job inline, so the
    wall clock here is the bulk pass's own time; the five stages named in
    `bulk.run`'s docstring are timed by patching the module-level function
    each calls, not by re-implementing the pass."""
    from plexora.agent import AgentSession, invoke, jobs, registry
    from plexora.plugins.qc.server import candidates as cand
    from plexora.plugins.qc.server import checks_bulk, scan
    from plexora.plugins.qc.server import detectors as detectors_pkg
    from plexora.plugins.qc.server.cells import bulk as cell_bulk
    from tests.qc_fixtures import make_qc_project

    registry.discover(["roi", "qc"])
    make_qc_project(tmp_path, name="qcbulktiming", size=3072, grid=96,
                    artifacts=("blur_local", "saturation", "aggregates", "fold",
                              "tile_seams", "cycle_dropout"))

    timings = {}
    _timed(monkeypatch, scan, "load_or_run", timings)
    _timed(monkeypatch, detectors_pkg, "run_all", timings)
    _timed(monkeypatch, cand, "build", timings)
    _timed(monkeypatch, checks_bulk, "run", timings, key="checks_bulk")
    _timed(monkeypatch, cell_bulk, "run", timings, key="cell_bulk")

    session = AgentSession()
    started = time.monotonic()
    answer = invoke(session, "qc_session_start", {
        "project": "qcbulktiming", "map_cell_um": 25.0, "on_limit": "extend",
        "seed": 0, "agent": "bench_timing"})
    assert answer["ok"], answer.get("error")
    jobs.drain(600)
    total = time.monotonic() - started

    status = invoke(session, "qc_session_status",
                    {"session_id": answer["result"]["session_id"], "units": "all"})
    session.close()
    assert status["ok"], status.get("error")
    assert status["result"]["state"] not in ("bulk_running", "failed"), status["result"]

    # Every stage the bulk pass docstring names ran (a project with a cell
    # table and every check enabled reaches all five) and stayed within its
    # own generous budget.
    assert set(timings) == set(STAGE_BUDGETS_S), timings
    for stage, budget in STAGE_BUDGETS_S.items():
        assert timings[stage] < budget, (stage, timings)
    assert total < TOTAL_BUDGET_S, (total, timings)


def test_candidate_merge_does_not_blow_up_at_session_scale():
    """The live run's bulk job raised ~5,400 small masks from one 40-channel
    aggregate detector and merged them with whole-grid numpy per pair --
    minutes of silence at "scanned TubbIII". The merge is pruned to only the
    pairs whose boxes meet (`test_qc_live_run_fixes.
    test_the_candidate_merge_only_compares_places_that_meet` checks that the
    pruned merge still groups exactly as a brute pairwise one would, at a
    small scale); this checks it stays fast at a scale that size of run
    actually produces: 3,000 small candidates on a 600x600 grid."""
    from plexora.plugins.qc.server import candidates as cand
    from plexora.plugins.qc.server.detectors.base import Candidate

    rng = np.random.default_rng(7)
    grid_shape = (600, 600)
    classes = ("antibody_aggregate", "tissue_fold", "out_of_focus")
    raw = []
    for k in range(3000):
        mask = np.zeros(grid_shape, dtype=bool)
        y, x = rng.integers(0, grid_shape[0] - 6, 2)
        h, w = rng.integers(1, 5, 2)
        mask[y:y + h, x:x + w] = True
        raw.append(Candidate(detector="aggregate", detector_version="1",
                             class_hint=classes[k % len(classes)], scope_hint="channel",
                             channels=(f"M{k % 40}",), mask=mask,
                             score=float(rng.uniform(0.3, 1)), severity=0.5))

    started = time.monotonic()
    merged = cand.merge(raw)
    elapsed = time.monotonic() - started

    assert merged
    assert len(merged) <= len(raw)
    assert elapsed < 5.0, elapsed
