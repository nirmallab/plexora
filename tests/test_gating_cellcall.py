"""The cell model's call layer (`autogate/cellcall.py`): the crops it reads
are the crops it was trained on, the partners it reads are the labelling
pages' partners, the gate's call stands unless the model is confident, and a
marker runs end to end on a synthetic project -- without the real weights
(a stand-in predictor), and without the network (a local release server).
"""

import hashlib
import http.server
import os
import threading

import numpy as np
import pytest

from tests.autogate_fixtures import make_gating_project

pytestmark = pytest.mark.paid


# -- the crops ----------------------------------------------------------------------


def _zoom_reference(a, order):
    """How the training crops were resampled (scipy's zoom of the central
    8/12), the oracle for `cellcall.resample`."""
    from scipy import ndimage as ndi

    from plexora.plugins.gating.server.autogate.cellcall import CELLCALL

    side = CELLCALL["side"]
    n = a.shape[0]
    k = int(round(n * 8 / 12))
    o = (n - k) // 2
    window = a[o:o + k, o:o + k]
    if k == side:
        return window
    if order == 1:
        window = window.astype(np.float32)
    return ndi.zoom(window, side / k, order=order, prefilter=False)[:side, :side]


def test_resampling_is_scipys_zoom_bit_for_bit():
    from plexora.plugins.gating.server.autogate import cellcall

    rng = np.random.default_rng(2)
    # 168 px (a 112 px window) is a size where zoom's last coordinate lands a
    # hair past the edge and reads 0: the model learned that border.
    for n in list(range(40, 260, 7)) + [72, 168]:
        planes = rng.random((3, n, n)).astype(np.float32) * 1000
        labels = rng.integers(0, 9, (3, n, n))
        mine = cellcall.resample(planes)
        mine_labels = cellcall.resample_labels(labels)
        for i in range(3):
            np.testing.assert_allclose(mine[i], _zoom_reference(planes[i], 1), atol=1e-2)
            np.testing.assert_array_equal(mine_labels[i], _zoom_reference(labels[i], 0))


def _crop(cell_id, planes, labels):
    from plexora.agent.evidence.crops import Crop

    crop = Crop(cell_id, 0.0, 0.0, (0, 0, 72, 72), 0)
    crop.planes = planes
    crop.labels = labels
    return crop


def test_tensors_follow_the_channel_contract():
    from plexora.plugins.gating.server.autogate import cellcall

    side = cellcall.CELLCALL["side"]
    n = 72                                    # an exact 48 px window: no resampling
    yy, xx = np.mgrid[:n, :n]
    labels = np.zeros((n, n), np.int32)
    labels[(yy - 36) ** 2 + (xx - 36) ** 2 <= 36] = 7       # the cell
    labels[(yy - 36) ** 2 + (xx - 46) ** 2 <= 16] = 9       # a neighbour
    marker = np.where(labels == 7, 400.0, 20.0).astype(np.float32)
    co = np.full((n, n), 50.0, np.float32)
    crops = {7: _crop(7, {"CD8": marker, "CD3": co}, labels),
             8: _crop(8, {"CD8": marker}, None)}               # no mask: dropped
    levels = {"CD8": (20.0, 380.0), "CD3": (10.0, 40.0)}
    kept, x = cellcall.tensors(crops, [7, 8], "CD8", pixel_gate=100.0, levels=levels,
                               co=["CD3"], anti=[])
    assert kept == [7] and x.shape == (1, len(cellcall.CHANNELS), side, side)
    ch = {name: x[0, i] for i, name in enumerate(cellcall.CHANNELS)}
    window = slice(12, 60)
    own = labels[window, window] == 7
    np.testing.assert_array_equal(ch["own"] > 0, own)
    np.testing.assert_array_equal(ch["others"] > 0, labels[window, window] == 9)
    assert ch["edges"][own].sum() < own.sum() and ch["edges"].any()
    np.testing.assert_allclose(ch["m_gate"][own], np.log1p(4.0), rtol=1e-6)
    np.testing.assert_allclose(ch["m_img"][own], np.log1p(1.0), rtol=1e-6)
    np.testing.assert_allclose(ch["co1"], np.log1p(1.0), rtol=1e-6)
    assert not ch["co2"].any() and not ch["anti1"].any() and not ch["dna"].any()
    assert ch["has_co"].min() == 1.0 and ch["has_anti"].max() == 0.0


def test_the_pixel_gate_is_the_cut_that_best_reproduces_the_calls():
    from plexora.plugins.gating.server.autogate import cellcall

    values = np.r_[np.linspace(10, 40, 30), np.linspace(60, 90, 30)]
    calls = values > 50
    cut, agreement = cellcall.best_cut(values, calls)
    assert 40 <= cut < 60 and agreement == 1.0
    assert np.isnan(cellcall.best_cut(values[:5], calls[:5])[0])


# -- partners -----------------------------------------------------------------------


def test_the_partner_plan_reads_the_vocabularys_relations():
    from plexora.plugins.gating.server.autogate import cellcall

    entries = {
        "CD8": {"canonical": "CD8", "partners": [
            {"marker": "CD3", "relation": "subset", "confidence": "high"},
            {"marker": "CD3e", "relation": "subset", "confidence": "high"},
            {"marker": "CD4", "relation": "exclusive", "confidence": "moderate"}]},
        "CD3": {"canonical": "CD3", "partners": [
            {"marker": "CD45", "relation": "subset", "confidence": "high"},
            {"marker": "CD20", "relation": "exclusive", "confidence": "high"}]},
        "CD3e": {"canonical": "CD3"},
        "CD45": {"canonical": "CD45"}, "CD20": {"canonical": "CD20"}, "CD4": {"canonical": "CD4"},
        "PanCK": {"canonical": "PanCK", "partners": [
            {"marker": "CD8", "relation": "exclusive", "confidence": "high"}]},
    }
    present = {"CD3", "CD3e", "CD45", "CD20", "CD4", "PanCK"}
    plan = cellcall.partner_plan(entries, "CD8", present, {}, 2)
    co = [p["marker"] for p in plan if p["kind"] == "co"]
    anti = [p["marker"] for p in plan if p["kind"] == "anti"]
    # Its own superset first, then the superset's superset; never a channel
    # read as the same marker twice (CD3 and CD3e).
    assert co == ["CD3", "CD45"]
    # Exclusive either way, and those of its supersets.
    assert set(anti) <= {"CD4", "CD20", "PanCK"} and len(anti) == 2
    assert {"PanCK", "CD20"} == set(anti)            # high before moderate
    # A gated partner comes first, whatever its confidence.
    gated = cellcall.partner_plan(entries, "CD8", present, {}, 2, gated={"CD4"})
    assert [p["marker"] for p in gated if p["kind"] == "anti"][0] == "CD4"
    # A partner without an image channel is never planned.
    assert all(p["marker"] != "CD45" for p in
               cellcall.partner_plan(entries, "CD8", present - {"CD45"}, {}, 2))


# -- the rule -----------------------------------------------------------------------


def test_the_gate_stands_unless_the_model_is_confident():
    from plexora.agent.cell_exclusions import NEGATIVE, NOT_CALLED, POSITIVE
    from plexora.plugins.gating.server.autogate import cellcall

    gate = np.array([POSITIVE, POSITIVE, POSITIVE, NEGATIVE, NEGATIVE, NEGATIVE,
                     NOT_CALLED, POSITIVE, NEGATIVE], dtype=np.int8)
    rows = np.array([0, 1, 2, 3, 4, 5, 6])
    p = np.array([0.05, 0.3, 0.8, 0.95, 0.7, 0.2, 0.99])
    found = cellcall.decide(rows, p, gate[rows], confident=0.1)
    assert found["demoted"].tolist() == [0]           # confidently not own above the gate
    assert found["promoted"].tolist() == [3]          # confidently own below it
    assert found["uncertain"].tolist() == [1, 4]      # leaning against the gate, not confident
    call = cellcall.apply(gate, found)
    assert call.tolist() == [NEGATIVE, POSITIVE, POSITIVE, POSITIVE, NEGATIVE, NEGATIVE,
                             NOT_CALLED, POSITIVE, NEGATIVE]
    assert gate[0] == POSITIVE                        # a copy: the gate's calls untouched


# -- end to end -----------------------------------------------------------------------


class _Counting:
    """A stand-in model: own when the cell's mask is bright against its pixel
    gate; counts the crops it was shown."""

    def __init__(self):
        self.seen = 0

    def __call__(self, x):
        self.seen += len(x)
        own = x[:, 3] > 0
        inside = (x[:, 0] * own).sum(axis=(1, 2)) / np.maximum(own.sum(axis=(1, 2)), 1)
        return (1.0 / (1.0 + np.exp(-6.0 * (inside - np.log1p(1.0))))).astype(np.float32)


def _gates(ds, markers):
    """A gate inside each marker's positive population (its median), so
    plenty of cells sit near it."""
    from plexora.plugins.gating.server.autogate import cells as cellmod

    out = {}
    for name in markers:
        values = cellmod.values(ds, name)
        lo, hi = np.nanpercentile(values, [5, 95])
        middle = np.sqrt(max(lo, 1.0) * hi)
        out[name] = float(np.nanmedian(values[values > middle]))
    return out


def test_a_marker_runs_end_to_end_and_is_stored_per_gate(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.gating.server.autogate import cellcall

    made = make_gating_project(tmp_path, "cc", grid=16, size=768)
    session = AgentSession()
    try:
        ds = session.data(made["name"])
        gates = _gates(ds, ("CD3", "CD8", "CD20"))
        model = _Counting()
        found = cellcall.run(session, ds, "CD8", gates["CD8"], gates=gates, model=model)
        info = found["info"]
        assert info["n_run"] == len(found["rows"]) == len(found["p"]) == model.seen > 0
        assert info["n_run"] <= info["n_within"] and np.isfinite(info["pixel_gate"])
        # The gate's calls on the cells read: both sides of it (no upper bound
        # is open above, never NaN).
        assert set(np.unique(found["run_call"]).tolist()) == {0, 1}
        assert info["reach_halves"] <= cellcall.CELLCALL["band_halves"]
        assert any(p["marker"] == "CD3" and p["kind"] == "co" for p in info["partners"])
        summary = cellcall.summary(found)
        moved = summary["n_promoted"] + summary["n_demoted"] + summary["n_uncertain"]
        assert summary["n_run"] == info["n_run"] and moved <= info["n_run"]
        # Stored per gate, model and data, in the user's agent folder.
        assert len(list(tmp_path.rglob("cellcall/*.npz"))) == 1
        again = cellcall.run(session, ds, "CD8", gates["CD8"], gates=gates, model=model)
        assert model.seen == info["n_run"]                     # read back, not recomputed
        np.testing.assert_array_equal(again["rows"], found["rows"])
        assert cellcall.load(ds, "CD8", gates["CD8"]) is not None
        assert cellcall.load(ds, "CD8", gates["CD8"] * 1.1) is None
        cellcall.run(session, ds, "CD8", gates["CD8"] * 1.1, gates=gates, model=model)
        assert model.seen > info["n_run"]                      # a new gate is a new run
        # The old one kept beside it (a session reads a marker at more than
        # one gate), up to `keep_runs`.
        assert len(list(tmp_path.rglob("cellcall/CD8_*.npz"))) == 2
    finally:
        session.close()


def test_without_a_mask_the_model_says_why_it_cannot_call(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.gating.server.autogate import cellcall

    made = make_gating_project(tmp_path, "nomask", grid=12, size=512, mask=False)
    session = AgentSession()
    try:
        ds = session.data(made["name"])
        gates = _gates(ds, ("CD3", "CD8"))
        with pytest.raises(cellcall.Unavailable, match="segmentation mask"):
            cellcall.run(session, ds, "CD8", gates["CD8"], gates=gates, model=_Counting())
    finally:
        session.close()


# -- the weights ----------------------------------------------------------------------


@pytest.fixture
def tiny_release(monkeypatch, tmp_path):
    """A stand-in model file served over HTTP, pinned as MODEL_FILES."""
    from plexora.plugins.gating.server.autogate import cellcall_weights

    served = tmp_path / "release"
    served.mkdir()
    data = np.random.default_rng(1).integers(0, 255, 50_000, dtype=np.uint8).tobytes()
    (served / "cellcall_v1.onnx").write_bytes(data)
    files = {"cellcall_v1.onnx": {"role": "model", "sha256": hashlib.sha256(data).hexdigest(),
                                  "bytes": len(data)}}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(served), **kwargs)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(cellcall_weights, "MODEL_FILES", files)
    monkeypatch.setenv(cellcall_weights.ENV_MODEL_URL, f"http://127.0.0.1:{server.server_port}/")
    monkeypatch.setenv(cellcall_weights.ENV_MODEL_DIR, str(tmp_path / "models"))
    yield served
    server.shutdown()


def test_the_weights_are_fetched_once_and_verified(tiny_release):
    from plexora.plugins.gating.server.autogate import cellcall_weights

    status = cellcall_weights.status()
    assert not status["ready"] and status["source"] == "environment"
    path = cellcall_weights.ensure()
    assert path.exists() and cellcall_weights.status()["ready"]
    # A file that changed on disk is never loaded: it is fetched again.
    path.write_bytes(b"x" * path.stat().st_size)
    os.utime(path, ns=(1, 1))
    assert cellcall_weights.missing(deep=True)
    cellcall_weights.ensure()
    assert not cellcall_weights.missing(deep=True)


def test_a_release_that_is_not_there_says_the_model_is_missing(tiny_release, monkeypatch):
    from plexora.plugins.gating.server.autogate import cellcall_weights

    (tiny_release / "cellcall_v1.onnx").unlink()
    with pytest.raises(cellcall_weights.CellModelMissing):
        cellcall_weights.ensure()
    assert not list((tiny_release.parent / "models").glob("*.part"))


@pytest.mark.skipif(not os.environ.get("PLEXORA_TEST_CELLCALL_MODEL_DIR"),
                    reason="needs the real weights (PLEXORA_TEST_CELLCALL_MODEL_DIR)")
def test_the_real_weights_answer_a_probability(monkeypatch):
    from plexora.plugins.gating.server.autogate import cellcall, cellcall_weights

    monkeypatch.setenv(cellcall_weights.ENV_MODEL_DIR, os.environ["PLEXORA_TEST_CELLCALL_MODEL_DIR"])
    assert not cellcall_weights.missing(deep=True)
    model = cellcall.Predictor(cellcall_weights.model_path())
    side = cellcall.CELLCALL["side"]
    p = model(np.zeros((3, len(cellcall.CHANNELS), side, side), np.float32))
    assert p.shape == (3,) and np.all((p >= 0) & (p <= 1))


# -- the tools, the route, the write ----------------------------------------------------


def _gated_project(tmp_path, name="cct"):
    """A synthetic project with CD3 and CD8 gated inside their positives."""
    from plexora.agent import AgentSession
    from plexora.plugins.gating.server import model

    made = make_gating_project(tmp_path, name, grid=16, size=768)
    session = AgentSession()
    ds = session.data(made["name"])
    gates = _gates(ds, ("CD3", "CD8"))
    for marker, low in gates.items():
        model.set_gate(ds, marker, low, model.get_gate(ds, marker)["default_high"])
    return made, session, gates


def test_the_tools_run_as_a_job_and_say_what_is_stored_per_gate(tmp_path, monkeypatch):
    from plexora.agent import invoke, registry
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import cellcall

    registry.discover(["gating"])
    stand_in = _Counting()
    monkeypatch.setattr(cellcall, "predictor", lambda: stand_in)
    made, session, gates = _gated_project(tmp_path)
    try:
        before = invoke(session, "get_cell_model_calls", {"project": made["name"]})
        assert before["ok"], before
        assert {m: r["state"] for m, r in before["result"]["markers"].items()} == \
            {"CD3": "not_run", "CD8": "not_run"}
        submitted = invoke(session, "run_cell_model", {"project": made["name"],
                                                       "markers": ["CD8", "CD20"]})
        assert submitted["ok"], submitted
        for _ in range(4):
            waited = invoke(session, "job_wait", {"job_id": submitted["result"]["job_id"],
                                                  "timeout_s": 60})
            assert waited["ok"], waited
            job = waited["result"]["job"]
            if job["status"] not in ("queued", "running"):
                break
        assert job["status"] == "done", job
        rows = job["result"]["markers"]
        assert rows["CD20"] == {"state": "not_gated"}
        assert rows["CD8"]["state"] == "ready" and rows["CD8"]["n_run"] == stand_in.seen > 0
        assert "never applied" in job["result"]["rule"]
        after = invoke(session, "get_cell_model_calls",
                       {"project": made["name"], "marker": "CD8", "cells": True})["result"]
        row = after["markers"]["CD8"]
        assert row["state"] == "ready" and row["n_run"] == rows["CD8"]["n_run"]
        assert {"promoted_cells", "demoted_cells", "uncertain_cells"} <= set(row)
        # The gate moved: what was stored was the old gate's.
        ds = session.data(made["name"])
        model.set_gate(ds, "CD8", gates["CD8"] * 1.05, model.get_gate(ds, "CD8")["high"])
        moved = invoke(session, "get_cell_model_calls", {"project": made["name"]})["result"]
        assert moved["markers"]["CD8"]["state"] == "not_run"
    finally:
        session.close()


def test_the_route_sends_one_code_a_row_and_the_counts(tmp_path, monkeypatch):
    import gzip

    from flask import Flask

    from plexora.plugins.gating.server import routes
    from plexora.plugins.gating.server.autogate import cellcall

    made, session, gates = _gated_project(tmp_path, "ccr")
    try:
        ds = session.data(made["name"])
        from plexora.plugins.gating.server import model

        gate = model.get_gate(ds, "CD8")
        found = cellcall.run(session, ds, "CD8", gate["low"], gate["high"], gates=gates,
                             model=_Counting())
        n = int(ds.table.geometry().height)
        expected, counts = cellcall.codes(n, found)
    finally:
        session.close()
    routes._ROUTE_SESSION.pop("session", None)
    app = Flask(__name__)
    app.register_blueprint(routes.gating_bp, url_prefix="/plugins/gating")
    client = app.test_client()
    reply = client.get("/plugins/gating/get_cell_model",
                       query_string={"datasource": made["name"], "marker": "CD8"})
    assert reply.status_code == 200
    body = np.frombuffer(gzip.decompress(reply.data), dtype=np.uint8)
    assert reply.headers["X-Cell-Model-State"] == "ready"
    np.testing.assert_array_equal(body, expected)
    assert int(reply.headers["X-Cell-Model-Own"]) == counts["own"] == int((body == 3).sum())
    assert int(reply.headers["X-Cell-Model-Not-Own"]) == counts["not_own"] == int((body == 2).sum())
    assert int(reply.headers["X-Cell-Model-Uncertain"]) == int((body == 1).sum())
    untouched = client.get("/plugins/gating/get_cell_model",
                           query_string={"datasource": made["name"], "marker": "CD3"})
    assert untouched.headers["X-Cell-Model-State"] == "not_run"
    assert not np.frombuffer(gzip.decompress(untouched.data), dtype=np.uint8).any()
    routes._ROUTE_SESSION.pop("session", None)


def test_the_model_calls_are_written_beside_the_gates(tmp_path):
    from types import SimpleNamespace

    import anndata as ad
    import pandas as pd

    from plexora.plugins.gating.server import anndata_gates

    obs = pd.DataFrame({"imageid": ["img"] * 6}, index=[f"c{i}" for i in range(6)])
    path = tmp_path / "cells.h5ad"
    ad.AnnData(X=np.zeros((6, 2), np.float32), obs=obs).write_h5ad(path)
    source = SimpleNamespace(path=str(path), kind="anndata", subset=None)
    written = anndata_gates.save_model_calls(source, "img", {"CD8": [1, 4]}, {"CD8": [2]}, 6,
                                             table_name="gates", imageid_column="imageid")
    assert written["model_own_layer"] == "obsm/gates_model_own"
    assert written["n_model_own"] == {"CD8": 2} and written["n_model_not_own"] == {"CD8": 1}
    back = ad.read_h5ad(path)
    assert back.obsm["gates_model_own"]["CD8"].tolist() == [False, True, False, False, True, False]
    assert back.obsm["gates_model_not_own"]["CD8"].tolist() == [False, False, True, False, False,
                                                                False]
