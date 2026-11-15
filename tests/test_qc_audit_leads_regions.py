"""Two QC write/read races from the Nov 2026 audit (pg-qc-a-8, pg-qc-c-1).

- A check's `write_regions` that fails part-way must not leave ROIs without
  `roi_meta` rows: `roi_link.sync` would adopt them as user-drawn, approved
  regions that no later write of the check ever replaces.
- The exclusions record gating reads (`server/exclusions.py`) must not be
  cached under a file token read after a region was drawn mid-derivation, or
  gating keeps the old exclusions until something else is written.
"""

import json

import numpy as np
import pytest

from plexora.agent import AgentSession, cell_exclusions, invoke, registry
from tests.test_gating_qc_exclusion import _box, _fold_project


@pytest.fixture(autouse=True)
def _plugins():
    from plexora.plugins.qc.server import exclusions

    registry.discover(["roi", "qc", "gating"])
    cell_exclusions._reset_for_tests()
    exclusions._FAST.clear()
    exclusions._CACHE.clear()
    yield
    cell_exclusions._reset_for_tests()
    exclusions._FAST.clear()
    exclusions._CACHE.clear()


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _call(session):
    from plexora.agent.audit import AuditLog
    from plexora.agent.policy import Policy
    from plexora.agent.receipts import operation_id
    from plexora.agent.registry import Call

    return Call(capability=registry.get("qc.blur_write_regions"), session=session,
                policy=Policy(), operation_id=operation_id(), audit=AuditLog(),
                arguments={}, project_name="gsynth")


def _record(key, box):
    from plexora.plugins.qc.server import region_write

    return region_write.check_candidate(
        "blur_test", "1", "out_of_focus", key, {"geometry": _box(*box), "max": 1.0},
        channels=["CD3"], scope="channel_specific", threshold={"threshold": 0.5})


def _write(session, work):
    from plexora.plugins.qc.server import region_write

    return region_write.write_regions(
        _call(session), "gsynth", detector="blur_test", klass="out_of_focus",
        covered={"CD3"}, work=work, row_keys=lambda row: row.get("channels") or [],
        cells=False)


def test_a_write_that_fails_part_way_leaves_no_region_sync_adopts_as_the_users(
        tmp_path, monkeypatch):
    from plexora.plugins.qc.server import results, roi_link

    _fold_project(tmp_path)
    session = AgentSession()
    real_create = roi_link.create
    made = []

    def create(ds, record, **kw):
        if made:
            raise RuntimeError("disk full")
        out = real_create(ds, record, **kw)
        made.append(out[2]["id"])
        return out

    monkeypatch.setattr(roi_link, "create", create)
    with pytest.raises(RuntimeError):
        _write(session, [("CD3", [_record("a", (0, 0, 200, 200)),
                                  _record("b", (300, 300, 500, 500))])])
    monkeypatch.setattr(roi_link, "create", real_create)
    assert made

    ds = session.image_data("gsynth")
    with results.lock("gsynth"):
        report = roi_link.sync(ds, results.load("gsynth"))
    assert not set(made) & set(report["adopted"]), report
    rows = {r["roi_id"]: r for r in results.roi_meta("gsynth").to_dicts()}
    live = {f["id"] for f in roi_link._features(roi_link._repo(ds).load())}
    for roi_id in set(made) & live:
        assert rows.get(roi_id, {}).get("created_by") != "user", rows.get(roi_id)

    # The next write of the same check replaces whatever the failed one left.
    written, _kept, removed, *_ = _write(session, [("CD3", [_record("c", (0, 0, 200, 200))])])
    live = {f["id"] for f in roi_link._features(roi_link._repo(ds).load())}
    assert not set(made) & live, "the failed write's region outlived the next write"
    assert written


def test_a_region_drawn_while_exclusions_derive_is_not_cached_as_seen(
        tmp_path, monkeypatch):
    from plexora.plugins.qc.server import exclusions

    _fold_project(tmp_path)
    session = AgentSession()
    ok(invoke(session, "create_roi", {"project": "gsynth", "category": "QC: Tissue fold",
                                      "geometry": _box(0, 0, 330, 330)}))
    real_record = exclusions._record
    drawn = []

    def record(ds, mode, document, result):
        out = real_record(ds, mode, document, result)
        if not drawn:  # a region drawn in the ROI panel while this derives
            drawn.append(ok(invoke(session, "create_roi", {
                "project": "gsynth", "category": "QC: Tissue fold",
                "geometry": _box(700, 700, 960, 960)})))
        return out

    monkeypatch.setattr(exclusions, "_record", record)
    first = cell_exclusions.current(session.data("gsynth"))
    monkeypatch.setattr(exclusions, "_record", real_record)
    assert drawn and first is not None

    after = cell_exclusions.current(session.data("gsynth"))
    exclusions._FAST.clear()
    exclusions._CACHE.clear()
    fresh = cell_exclusions.current(session.data("gsynth"))
    assert len(fresh.whole) > len(first.whole)
    assert np.array_equal(np.sort(after.whole), np.sort(fresh.whole)), (
        f"gating kept the exclusions from before the region was drawn: "
        f"{len(after.whole)} left out, {len(fresh.whole)} once re-derived")


def test_calls_derived_while_a_region_is_drawn_are_not_stamped_current(
        tmp_path, monkeypatch):
    """The stored stamp, not only the cache: calls derived from the regions
    before one was drawn must not carry the ROI revision after it, or gating
    never derives again for that region (whatever is cached)."""
    from plexora.plugins.qc.server import exclusions
    from plexora.plugins.qc.server.cells import calls

    _fold_project(tmp_path)
    session = AgentSession()
    ok(invoke(session, "create_roi", {"project": "gsynth", "category": "QC: Tissue fold",
                                      "geometry": _box(0, 0, 330, 330)}))
    real_derive = calls.derive
    drawn = []

    def derive(*args, **kwargs):
        out = real_derive(*args, **kwargs)
        if not drawn:
            drawn.append(ok(invoke(session, "create_roi", {
                "project": "gsynth", "category": "QC: Tissue fold",
                "geometry": _box(700, 700, 960, 960)})))
        return out

    monkeypatch.setattr(calls, "derive", derive)
    first = cell_exclusions.current(session.data("gsynth"))
    monkeypatch.setattr(calls, "derive", real_derive)
    assert drawn and first is not None

    exclusions._FAST.clear()
    exclusions._CACHE.clear()
    after = cell_exclusions.current(session.data("gsynth"))
    assert len(after.whole) > len(first.whole), (
        "the calls derived before the region was drawn were stamped as current")
