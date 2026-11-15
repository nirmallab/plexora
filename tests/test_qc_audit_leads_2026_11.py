"""QC leads from the Nov 2026 audit (docs/internal/AUDIT_2026-11-14.md §7),
each pinned by the symptom it had:

- flow-user-state-4: on Windows the QC store's cross-process lock gave up
  after `msvcrt.LK_LOCK`'s ten one-second tries and the write went on
  unlocked, silently;
- pg-qc-a-2: a cyclic image's registration fields were swept off disk four at
  a time, so a session with five or more DNA cycles lost its review;
- pg-qc-a-3: "Save into file" changed the table's mtime, which made QC's own
  calls stale (a full re-propagation of every region on the next read);
- pg-qc-a-5: the SpatialData regions element was scaled by 1/scale for a mask
  drawn from polygons, onto a labels element that is not that mask;
- pg-qc-a-6: with no stored scan, the tissue mask was read from the image on
  every derive;
- pg-qc-b-4: a cycle out of register everywhere (a channel-level verdict) was
  capped at warn by the large-region rule, unlike a failed stain;
- pg-qc-b-5: channels a written region took in by a merge or an absorb never
  reached the stored result the cells follow;
- pg-qc-b-6: a bulk pass that failed with anything but the scan's AgentError
  left the session `bulk_running`, and every qc_next resubmitted it;
- pg-qc-c-2: `frames.locate_box` refused every box on a render_region picture;
- pg-qc-c-3: `overview.rank_channels` raised KeyError on a tie at the cut-off.
"""

import contextlib
import os
import types

import numpy as np
import pytest

from tests.qc_fixtures import make_qc_project

pytestmark = pytest.mark.pro


# -- pg-qc-c-2 ------------------------------------------------------------------------------


def test_a_box_on_a_single_render_is_located(monkeypatch):
    from plexora.plugins.qc.server import frames

    manifest = {"bounds_fullres": {"x": 100, "y": 50, "width": 400, "height": 200},
                "output_size": [200, 100]}
    monkeypatch.setattr(frames, "_load", lambda project, artifact_id: manifest)
    box, _manifest = frames.locate_box("p", "art_1", (10, 10, 60, 30))
    assert box == {"x": 120.0, "y": 70.0, "width": 100.0, "height": 40.0}


# -- pg-qc-c-3 ------------------------------------------------------------------------------


def test_channels_tied_at_the_cut_off_are_shown_from_the_planes_held(tmp_path, monkeypatch):
    from plexora.agent import AgentSession, registry
    from plexora.plugins.qc.server import overview
    from tests import qc_fixtures

    registry.discover(["roi", "qc"])
    scene = qc_fixtures.qc_scene

    def tied(*args, **kwargs):
        image, labels, cells, channels, truth = scene(*args, **kwargs)
        at = list(channels).index("CD8")
        copies = np.stack([image[at], image[at]])
        return (np.concatenate([image, copies]), labels, cells,
                [*channels, "CD8b", "CD8c"], truth)

    monkeypatch.setattr(qc_fixtures, "qc_scene", tied)
    info = make_qc_project(tmp_path, size=512, grid=20, artifacts=("aggregates",))
    region = next(r for r in info["truth"]["regions"] if r["name"] == "aggregates")
    ys, xs = np.nonzero(region["mask"])
    cx, cy = float(xs.mean()), float(ys.mean())
    box = {"x": cx - 40, "y": cy - 40, "width": 80, "height": 80}
    ranking = overview.rank_channels(AgentSession(), "qcsynth", box, top=2)
    assert len(ranking["shown"]) == 2
    assert set(ranking["shown"]) <= {"CD8", "CD8b", "CD8c"}
    assert set(ranking["planes"]) == set(ranking["shown"])


# -- pg-qc-b-6 ------------------------------------------------------------------------------


def test_a_bulk_pass_that_raises_fails_the_session(monkeypatch):
    from plexora.plugins.qc.server import bulk

    record = {"state": "created"}

    @contextlib.contextmanager
    def engine_for(call, session_id):
        yield types.SimpleNamespace(record=record)

    def boom(call, session_id, *args):
        record["state"] = "bulk_running"
        raise ValueError("the overview could not be read")

    monkeypatch.setattr(bulk, "engine_for", engine_for)
    monkeypatch.setattr(bulk, "_progress_announcer", lambda call, sid: lambda *a, **k: None)
    monkeypatch.setattr(bulk, "_run_bulk", boom)
    with pytest.raises(ValueError):
        bulk.run(types.SimpleNamespace(), types.SimpleNamespace(session_id="qs_1"))
    assert record["state"] == "failed"
    assert "overview" in record["error"]["message"]


def test_a_cancelled_bulk_pass_is_not_a_failure(monkeypatch):
    from plexora.agent.jobs import JobCancelled
    from plexora.plugins.qc.server import bulk

    record = {"state": "bulk_running"}

    @contextlib.contextmanager
    def engine_for(call, session_id):
        yield types.SimpleNamespace(record=record)

    def stopped(*args):
        raise JobCancelled("stopped from the viewer")

    monkeypatch.setattr(bulk, "engine_for", engine_for)
    monkeypatch.setattr(bulk, "_progress_announcer", lambda call, sid: lambda *a, **k: None)
    monkeypatch.setattr(bulk, "_run_bulk", stopped)
    with pytest.raises(JobCancelled):
        bulk.run(types.SimpleNamespace(), types.SimpleNamespace(session_id="qs_1"))
    assert record["state"] == "bulk_running"


# -- flow-user-state-4 ----------------------------------------------------------------------


class _FakeMsvcrt:
    """`msvcrt` as a held lock looks: LK_LOCK gives up (its ten one-second
    tries), the polled LK_NBLCK gets it on the third go."""
    LK_LOCK, LK_NBLCK, LK_UNLCK = 1, 2, 0

    def __init__(self):
        self.tries = 0

    def locking(self, fd, mode, nbytes):
        if mode == self.LK_UNLCK:
            return
        if mode == self.LK_LOCK:
            raise OSError(36, "Resource deadlock avoided")
        self.tries += 1
        if self.tries < 3:
            raise OSError(13, "Permission denied")


def test_the_qc_store_lock_waits_on_windows_rather_than_going_on_unlocked(
        tmp_path, monkeypatch):
    from plexora import _process_lock
    from plexora.plugins.qc.server import results

    make_qc_project(tmp_path, artifacts=(), table=False, mask=False)
    fake = _FakeMsvcrt()
    for module in (results, _process_lock):
        monkeypatch.setattr(module, "_fcntl", None, raising=False)
        monkeypatch.setattr(module, "_msvcrt", fake, raising=False)
    lock = results.lock("qcsynth_win_lock")
    monkeypatch.setattr(lock, "_path_fn", lambda: tmp_path / "qc.lock", raising=False)
    monkeypatch.setattr(lock, "_path", lambda: tmp_path / "qc.lock")
    with lock:
        assert lock._handle is not None
    assert fake.tries == 3


# -- pg-qc-a-2 ------------------------------------------------------------------------------


def _entry(seed):
    rng = np.random.default_rng(seed)
    grid = rng.random((4, 4)).astype(np.float32)
    field = {"dy": grid, "dx": grid, "confidence": grid, "tissue": grid > 0.5,
             "global": {"dy": 0.0, "dx": 0.0}, "grid": {"block_px": 64},
             "map": {"grid": {"cell_px": 10}, "share": grid, "nucleus": grid,
                     "reference": grid, "comparison": grid}}
    return {"field": field, "factor": 4.0, "level": 2, "computed_at": "now"}


def test_every_cycle_s_registration_field_outlives_the_sweep(tmp_path, monkeypatch):
    from plexora.plugins.qc.server import registration

    folder = tmp_path / "registration"
    folder.mkdir()
    monkeypatch.setattr(registration, "_folder", lambda project: folder)
    fps = [f"fp{i:02d}" for i in range(10)]
    for i, fp in enumerate(fps):
        registration._save_field("p", fp, _entry(i))
        registration._note_last("p", {"reference": "DNA_1", "comparison": f"DNA_{i + 2}"},
                                64, fp)
        os.utime(folder / f"{fp}.json", ns=(10**18 + i * 10**9, 10**18 + i * 10**9))
    with registration._GUARD:
        registration._FIELDS.clear()
    assert all(registration.field_entry("p", fp) is not None for fp in fps)
    # A field measured again under the same pair replaces the old one, which
    # then goes once four newer stale ones exist.
    for j in range(6):
        fp = f"redo{j}"
        registration._save_field("p", fp, _entry(100 + j))
        registration._note_last("p", {"reference": "DNA_1", "comparison": "DNA_2"}, 64, fp)
    assert not (folder / "fp00.json").exists()
    assert (folder / "redo5.json").exists() and (folder / "fp09.json").exists()


# -- pg-qc-a-3 ------------------------------------------------------------------------------


def test_saving_calls_into_the_table_does_not_make_them_stale(tmp_path, monkeypatch):
    from plexora.agent import AgentSession
    from plexora.plugins.qc.server import results, source_write, tableops
    from plexora.plugins.qc.server.cells import inputs as cell_inputs

    make_qc_project(tmp_path, artifacts=())
    ds = AgentSession().data("qcsynth")
    before = cell_inputs.fingerprints(ds)
    with results.lock("qcsynth"):
        document = results.load("qcsynth")
        result = results.new_result("qcsynth", session_id="qs_t")
        result["cells"] = {"n": 1, "inputs": before}
        results.put_result(document, result, activate=True)
        results.save("qcsynth", document)
    src = ds.project.dataset.src

    def run_write(dataset, payload):
        stat = os.stat(src)
        os.utime(src, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5 * 10**9))
        return {"path": src, "columns": []}

    monkeypatch.setattr(source_write, "gather", lambda ds, **kw: {"image_id": "qcsynth"})
    monkeypatch.setattr(tableops, "run_write", run_write)
    source_write.write(ds, replace=True)
    now = cell_inputs.fingerprints(ds)
    assert now["table_fp"] != before["table_fp"]
    stored = results.active(results.load("qcsynth"))["cells"]["inputs"]
    assert not cell_inputs.needs_full(stored, now)


# -- pg-qc-a-5 ------------------------------------------------------------------------------


def test_regions_of_a_polygon_drawn_mask_are_placed_by_the_image(tmp_path, monkeypatch):
    spatialdata = pytest.importorskip("spatialdata")
    from spatialdata.models import Image2DModel, Labels2DModel
    from spatialdata.transformations import Identity

    from plexora.plugins.qc.server import spatialdata_write
    from plexora.plugins.roi.server import adapters

    image = Image2DModel.parse(np.zeros((1, 32, 32), dtype=np.uint8), dims=("c", "y", "x"),
                               transformations={"global": Identity()})
    # A nucleus raster on the image's own grid: not the polygon mask, which is
    # drawn twice as fine (seg_scale 2) and is no element of the store.
    labels = Labels2DModel.parse(np.zeros((32, 32), dtype=np.int32), dims=("y", "x"),
                                 transformations={"global": Identity()})
    store = tmp_path / "s.zarr"
    spatialdata.SpatialData(images={"img": image}, labels={"nuclei": labels}).write(store)
    dataset = types.SimpleNamespace(project=types.SimpleNamespace(
        image=types.SimpleNamespace(src=str(store / "images" / "img")),
        segmentation=types.SimpleNamespace(source=str(store / "shapes" / "cells"),
                                           derived=None)))
    monkeypatch.setattr(adapters, "_spatialdata_store", lambda ds: str(store))
    found, _note = spatialdata_write._transformations(dataset, str(store), 2)
    point = np.array([[10.0, 6.0]])
    moved = found["global"].to_affine_matrix(input_axes=("x", "y"), output_axes=("x", "y"))
    out = moved @ np.array([point[0, 0], point[0, 1], 1.0])
    assert out[:2] == pytest.approx([10.0, 6.0])


# -- pg-qc-a-6 ------------------------------------------------------------------------------


def test_the_tissue_mask_is_read_once_without_a_stored_scan(tmp_path, monkeypatch):
    from plexora.agent import AgentSession
    from plexora.plugins.qc.server import scan, tissue

    make_qc_project(tmp_path, artifacts=())
    reads = []
    real = scan._read

    def counted(*args, **kwargs):
        reads.append(args[1])
        return real(*args, **kwargs)

    monkeypatch.setattr(scan, "_read", counted)
    session = AgentSession()
    first = tissue.for_project(session, "qcsynth")
    n = len(reads)
    assert n >= 1
    second = tissue.for_project(session, "qcsynth")
    assert len(reads) == n
    assert np.array_equal(first["mask"], second["mask"])
    second["mask"][:] = False  # a caller's change never reaches the next one
    assert tissue.for_project(session, "qcsynth")["mask"].any()


# -- pg-qc-b-4 ------------------------------------------------------------------------------


def test_a_cycle_out_of_register_everywhere_is_not_capped_as_a_large_region():
    from plexora.plugins.qc.server import strictness
    from plexora.plugins.qc.server.engine import QCEngine

    fake = types.SimpleNamespace(scan=lambda project: types.SimpleNamespace(
        meta={"tissue": {"area_px": 1000.0}}))
    unit = {"project": "p", "trace": "none", "whole_tissue": True, "channel_level": True,
            "class_hint": "cross_cycle_registration_error",
            "measurement": {"tissue_fraction": 1.0}}
    envelope = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10],
                                                    [0, 0]]]}
    QCEngine._map_outline(fake, unit, envelope)
    assert unit["measurement"]["refined_fraction"] == 0.0
    decision = {"artifact_class": "cross_cycle_registration_error", "severity": "severe",
                "confidence": "fairly_sure"}
    found = strictness.decide_artifact(decision, unit["measurement"],
                                       strictness.thresholds("standard"))
    assert found["action"] == "exclude", found


# -- pg-qc-b-5 ------------------------------------------------------------------------------


def test_channels_a_written_region_absorbs_reach_the_stored_result(tmp_path):
    from plexora.plugins.qc.server import results, transitions
    from plexora.plugins.qc.server.engine import QCEngine

    make_qc_project(tmp_path, artifacts=(), table=False, mask=False)
    with results.lock("qcsynth"):
        document = results.load("qcsynth")
        result = results.new_result("qcsynth", session_id="qs_m")
        result["candidates"] = {"c1": {"id": "c1", "roi_id": "r1", "channels": ["CD3"],
                                       "class": "aggregate", "action": "exclude"}}
        results.put_result(document, result, activate=True)
        results.save("qcsynth", document)
    decided = {"type": "candidate", "project": "qcsynth", "id": "c1", "roi_id": "r1",
               "state": "confirmed_exclude", "class": "aggregate", "channels": ["CD3"]}
    other = {"type": "candidate", "project": "qcsynth", "id": "c2", "state": "awaiting_audit",
             "class_hint": "aggregate", "channels": ["CD8"], "level": 0}
    mask = np.ones((4, 4), dtype=bool)
    fake = types.SimpleNamespace(
        project="qcsynth", record={"result_id": result["result_id"]},
        units_of=lambda type_, project=None: [decided, other],
        mask_of=lambda unit: mask, close=lambda unit, state, reason: unit.update(state=state))
    fake.store_channels = types.MethodType(QCEngine.store_channels, fake)
    transitions._absorb(fake, decided)
    assert other["state"] == "merged"
    stored = results.active(results.load("qcsynth"))["candidates"]["c1"]
    assert stored["channels"] == ["CD3", "CD8"]
