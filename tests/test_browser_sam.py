"""Magic select on the website: the model in the page, everything else Python's.

On the website Plexora's Python (Pyodide) has no ONNX Runtime; the page runs
MobileSAM on onnxruntime-web and Python asks it for exactly the two runs, its
setup and its status (plexora/browser/sam_page.py, `sam_backend.Executor`).
These drive that seam under CPython with a stand-in page (`Page`) answering
the "sam" questions as services/onnxExecutor.js does -- arrays as bytes,
embeddings kept under a key -- over a backend run here, and pin that:

- the status, setup and cancel the viewer's client reads are the desktop's
  states, from what the page says;
- a click through the page is the click run here, exactly, on the same
  backend (the real model too, with PLEXORA_TEST_SEGMENT_MODEL_DIR);
- a second click sends the embedding's key, not the embedding, and a page
  that forgot it is sent the embedding;
- a page without the model's files answers the route's 409 `not_ready`, so
  the client sets it up as it does on the desktop.
"""

import os
from pathlib import Path

import numpy as np
import pytest

from plexora.browser import sam_page
from plexora.vision import sam, sam_backend, sam_weights
from plexora.vision import segment as segmenter


def wire(array):
    """An array as it crosses: dtype, shape and its bytes."""
    array = np.ascontiguousarray(array)
    return {"dtype": str(array.dtype), "shape": list(array.shape), "data": array.tobytes()}


class Page:
    """The page's side of the "sam" questions, over `backend` (any
    `SamBackend`), keeping what onnxWorker.js keeps."""

    KEEP = 8

    def __init__(self, backend, stored=True, runtime=True):
        self.backend = backend
        self.stored = stored
        self.runtime = runtime
        self.download = None
        self.error = None
        self.asked = []
        self.embeddings = {}
        self.next = 1

    def __call__(self, kind, args):
        assert kind == "sam"
        self.asked.append((args["op"], {key: ("array" if isinstance(value, np.ndarray) else value)
                                        for key, value in args.items() if key != "op"}))
        return getattr(self, args["op"])(args)

    def ops(self):
        return [op for op, _ in self.asked]

    def status(self, args):
        if not self.runtime:
            return None
        files = {f["name"]: {"present": self.stored, "ok": self.stored, "bytes": f["bytes"]}
                 for f in args["model"]["files"]}
        return {"runtime": {"installed": True, "version": "onnxruntime-web test", "install": None},
                "weights": {"dir": "browser storage", "source": "browser", "files": files,
                            "missing": [] if self.stored else list(files)},
                "download": self.download, "error": self.error,
                "device": {"encoder": "wasm", "decoder": "wasm"}}

    def install(self, args):
        self.download = {"running": True, "done": 0, "total": sam_weights.TOTAL_BYTES,
                         "file": None, "error": None, "fraction": 0.0}
        return self.status(args)

    def cancel(self, args):
        self.download = dict(self.download or {}, running=False)
        return True

    def load(self, args):
        if not self.stored:
            return {"missing": [f["name"] for f in args["model"]["files"]]}
        return {"device": {"encoder": "wasm", "decoder": "wasm", "runtime": "onnxruntime-web test"}}

    def embed(self, args):
        embedding = self.backend.embed(args["image"])
        key = f"e{self.next}"
        self.next += 1
        self.embeddings[key] = np.array(embedding, np.float32)
        while len(self.embeddings) > self.KEEP:
            self.embeddings.pop(next(iter(self.embeddings)))
        return {"embedding": wire(embedding), "key": key, "ms": 1.0}

    def decode(self, args):
        key = args.get("key")
        held = self.embeddings.get(key) if key else None
        if key and held is None and args.get("embedding") is None:
            return {"missing": True}
        if held is None:
            held = np.array(args["embedding"], np.float32)
            key = f"e{self.next}"
            self.next += 1
            self.embeddings[key] = held
        iou, low = self.backend.decode(held, args["coords"], args["labels"], args["mask_input"],
                                       args["has_mask"])
        return {"iou": wire(iou), "low": wire(low), "key": key, "ms": 1.0}

    def remove(self, args):
        self.stored = False
        return {"removed": [f["name"] for f in args["model"]["files"]]}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(sam, "_BACKEND", None)
    monkeypatch.setattr(sam, "_SESSION_ERROR", None)
    monkeypatch.delenv(sam.ENV_SWITCH, raising=False)
    segmenter.forget()
    yield
    sam_backend.set_executor(None)
    segmenter.forget()


@pytest.fixture
def scene(tmp_path):
    from tests.qc_fixtures import make_qc_project

    return make_qc_project(tmp_path, size=512, grid=20, artifacts=("speck",))


def _speck_click(info):
    region = next(r for r in info["truth"]["regions"] if r["name"] == "speck")
    ys, xs = np.nonzero(region["mask"])
    cx, cy = float(xs.mean()), float(ys.mean())
    view = {"x": max(0.0, cx - 120), "y": max(0.0, cy - 120), "width": 240, "height": 240}
    return view, cx, cy


def _click(info, **extra):
    from plexora.agent import AgentSession

    view, cx, cy = _speck_click(info)
    return segmenter.segment(AgentSession(), "qcsynth", view=view,
                             points=[{"x": cx, "y": cy, "label": 1}],
                             channels=[{"name": info["channels"][0], "range": [0, 6000]}], **extra)


def _same_answer(here, there):
    for key in ("geometry", "bbox", "iou", "area_px2", "flags", "level", "crop", "prev_key", "token"):
        assert here[key] == there[key], key


# -- status and setup ------------------------------------------------------------


def test_the_status_is_the_desktops_states_from_what_the_page_says():
    page = Page(sam_backend.FakeSamBackend(), stored=False)
    sam_page.install(page)
    status = sam.status().to_dict()
    assert status["state"] == "weights_missing"
    assert set(status["weights"]["missing"]) == set(sam_weights.MODEL_FILES)
    assert status["runtime"]["installed"] and status["size_mb"] == round(sam_weights.TOTAL_BYTES / 1e6)
    assert status["model"]["name"] == sam_weights.MODEL_NAME
    # The page is handed the pins to fetch and check the files with.
    model = page.asked[0][1]["model"]
    assert model["dir"] == sam_weights.WEB_DIR
    assert {f["name"]: f["sha256"] for f in model["files"]} == {
        name: entry["sha256"] for name, entry in sam_weights.MODEL_FILES.items()}
    assert not sam.available()

    started = sam.start_install().to_dict()
    assert started["state"] == "downloading" and "install" in page.ops()
    page.download.update(done=page.download["total"] // 2, fraction=0.5)
    assert sam.status().to_dict()["download"]["fraction"] == 0.5
    assert sam.cancel_install().state == "weights_missing" and "cancel" in page.ops()

    page.download = {"running": False, "error": "mobile_sam_decoder.onnx did not match its checksum"}
    assert (sam.status().state, sam.status().hint) == ("error", page.download["error"])

    page.stored, page.download = True, None
    assert sam.status().state == "ready" and sam.available()
    page.error = "WebAssembly ran out of memory"
    assert (sam.status().state, sam.status().hint) == ("error", page.error)


def test_a_page_that_does_not_answer_is_not_a_runtime():
    sam_page.install(Page(None, runtime=False))
    status = sam.status()
    assert status.state == "not_installed_runtime"
    assert status.hint == sam_page.NO_PAGE


def test_switched_off_is_switched_off_here_too(monkeypatch):
    sam_page.install(Page(sam_backend.FakeSamBackend()))
    monkeypatch.setenv(sam.ENV_SWITCH, "0")
    assert sam.status().state == "disabled" and not sam.available()


def test_remove_asks_the_page():
    page = Page(sam_backend.FakeSamBackend())
    sam_page.install(page)
    assert set(sam.remove()) == set(sam_weights.MODEL_FILES)
    assert sam.status().state == "weights_missing"


# -- the click ---------------------------------------------------------------------


def test_a_click_through_the_page_is_the_click_run_here(scene):
    with sam.use_backend(sam_backend.FakeSamBackend()):
        here = _click(scene)
    segmenter.forget()
    page = Page(sam_backend.FakeSamBackend())
    sam_page.install(page)
    there = _click(scene)
    _same_answer(here, there)
    assert there["device"]["runtime"] == "onnxruntime-web test"
    assert page.ops() == ["load", "embed", "decode"]
    # The image crossed as an array, nothing else did.
    assert page.asked[1][1] == {"image": "array"}


def test_a_second_click_names_the_embedding_and_a_forgetful_page_is_sent_it(scene):
    page = Page(sam_backend.FakeSamBackend())
    sam_page.install(page)
    first = _click(scene)
    view, cx, cy = _speck_click(scene)
    from plexora.agent import AgentSession

    def again():
        return segmenter.segment(AgentSession(), "qcsynth", view=view,
                                 points=[{"x": cx, "y": cy, "label": 1},
                                         {"x": cx + 60, "y": cy + 60, "label": 0}],
                                 channels=[{"name": scene["channels"][0], "range": [0, 6000]}],
                                 token=first["token"])

    page.asked.clear()
    second = again()
    decodes = [args for op, args in page.asked if op == "decode"]
    assert [op for op, _ in page.asked if op == "embed"] == []
    assert len(decodes) == 1 and decodes[0]["key"]
    assert "embedding" not in decodes[0]
    # The page let it go (reloaded, or more views since): sent with the call.
    page.embeddings.clear()
    page.asked.clear()
    third = again()
    decodes = [args for op, args in page.asked if op == "decode"]
    assert len(decodes) == 2 and decodes[1]["embedding"] == "array"
    assert third["geometry"] == second["geometry"]
    # And named again from then on.
    page.asked.clear()
    again()
    assert [args.get("embedding") for op, args in page.asked if op == "decode"] == [None]
    assert first["token"] == second["token"] == third["token"]


def test_without_the_files_the_route_answers_not_ready(scene):
    import plexora

    page = Page(sam_backend.FakeSamBackend(), stored=False)
    sam_page.install(page)
    view, cx, cy = _speck_click(scene)
    client = plexora.app.test_client()
    answer = client.post("/segment/v1/segment", json={
        "datasource": "qcsynth", "view": view, "points": [{"x": cx, "y": cy, "label": 1}]})
    assert answer.status_code == 409
    body = answer.get_json()
    assert body["error"]["code"] == "not_ready" and body["error"]["state"] == "weights_missing"
    started = client.post("/segment/v1/install", json={})
    assert started.status_code == 202 and started.get_json()["state"] == "downloading"
    assert client.get("/segment/v1/status").get_json()["state"] == "downloading"


def test_the_page_missing_the_files_at_load_is_not_ready_either():
    page = Page(sam_backend.FakeSamBackend(), stored=True)
    sam_page.install(page)
    page.stored = False  # deleted after a failed check, between status and load
    with pytest.raises(sam_weights.SamWeightsMissing):
        sam.session()


# -- the real model -------------------------------------------------------------------

MODEL_DIR = os.environ.get("PLEXORA_TEST_SEGMENT_MODEL_DIR")


@pytest.mark.skipif(not MODEL_DIR, reason="PLEXORA_TEST_SEGMENT_MODEL_DIR names the MobileSAM files")
def test_the_real_model_through_the_page_is_the_real_model_here(scene):
    directory = Path(MODEL_DIR)
    backend = sam_backend.OnnxSamBackend(sam_weights.path_for("encoder", directory),
                                         sam_weights.path_for("decoder", directory), device="cpu")
    with sam.use_backend(backend):
        here = _click(scene)
    segmenter.forget()
    sam_page.install(Page(backend))
    there = _click(scene)
    _same_answer(here, there)
    assert there["geometry"] is not None
