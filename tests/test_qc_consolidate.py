"""The end of a session: one ROI per place, every finding kept behind it."""

import pytest

from plexora.agent import AgentSession, invoke, registry
from tests.qc_fixtures import make_qc_project
from tests.test_qc_session import QCOracle, drive, ok, start

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


def _finding(cid, klass, channels, geometry, state="confirmed_exclude"):
    return {"id": cid, "class_hint": klass, "decision": {"artifact_class": klass},
            "channels": channels, "geometry": geometry, "state": state}


def test_the_partition_gives_each_place_to_its_best_explanation():
    from shapely.geometry import shape

    from plexora.plugins.qc.server import consolidate

    fold = _finding("a", "tissue_fold", ["DNA_1", "CD3"], _box(0, 0, 100, 100))
    blur = _finding("b", "out_of_focus", ["DNA_1"], _box(50, 0, 150, 100))
    agg1 = _finding("c", "antibody_aggregate", ["CD3"], _box(300, 300, 340, 340))
    agg2 = _finding("d", "antibody_aggregate", ["CD3"], _box(400, 400, 440, 440))
    pieces = consolidate.plan([blur, agg1, fold, agg2])
    keys = [key for key, _piece, _units in pieces]
    # Physical damage first, then focus, then signal; the two aggregates are one.
    assert keys == [("tissue_fold", ("CD3", "DNA_1")), ("out_of_focus", ("DNA_1",)),
                    ("antibody_aggregate", ("CD3",))]
    fold_piece, blur_piece, agg_piece = (p for _k, p, _u in pieces)
    assert fold_piece.area == pytest.approx(10_000)
    assert blur_piece.area == pytest.approx(5_000)          # only what the fold left
    assert agg_piece.geom_type == "MultiPolygon" and agg_piece.area == pytest.approx(3_200)
    for i, (_k, a, _u) in enumerate(pieces):
        for _k2, b, _u2 in pieces[i + 1:]:
            assert a.intersection(b).area == pytest.approx(0, abs=1e-6)
    assert shape(_box(0, 0, 150, 100)).difference(fold_piece.union(blur_piece)).area < 1e-6


def test_nothing_to_consolidate_changes_nothing():
    from plexora.plugins.qc.server import consolidate

    apart = [_finding("a", "tissue_fold", ["DNA_1"], _box(0, 0, 100, 100)),
             _finding("b", "out_of_focus", ["DNA_1"], _box(500, 500, 600, 600)),
             # a sliver of contact is edge noise, not a shared place
             _finding("c", "antibody_aggregate", ["CD3"], _box(99, 0, 300, 100))]
    assert consolidate.plan(apart) == []


def test_a_session_ends_with_non_overlapping_rois_and_every_finding_kept(tmp_path):
    from shapely.geometry import shape

    from plexora.plugins.qc.server import results, roi_link
    from plexora.plugins.qc.server.engine import store

    info = make_qc_project(tmp_path, artifacts=("aggregates", "misregistration",
                                                "blur_local", "saturation"))
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], QCOracle(info))
    ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    record = store().load(started["session_id"])
    ds = session.image_data("qcsynth")
    active = results.active(results.load("qcsynth"))
    live = roi_link.live_regions(ds, active)
    assert live
    shapes = [shape(r["geometry"]).buffer(0) for r in live]
    for i, a in enumerate(shapes):
        for b in shapes[i + 1:]:
            smaller = min(a.area, b.area) or 1.0
            assert a.intersection(b).area < 0.05 * smaller
    summary = record.get("consolidation")
    if summary:
        candidates = active["candidates"]
        merged = [c for c in candidates.values() if c.get("consolidated_into")]
        assert merged and all(not c.get("roi_id") for c in merged)
        for piece in summary["pieces"]:
            held = candidates[piece["id"]]
            assert held["roi_id"] and held["findings"]
            assert set(held["channels"]) >= {c for f in held["findings"]
                                             for c in f["channels"]}
        # The cells still follow each finding: a member key per finding.
        keys = set(roi_link.membership_meta(active))
        assert any("#" in k for k in keys)
    print("CONSOLIDATION", summary, len(live))
    # Every finding here is marker-level: the cells keep their markers' flags.
    cells = results.cells("qcsynth")
    assert cells is not None and cells["marker_flags"].list.len().sum() > 0
