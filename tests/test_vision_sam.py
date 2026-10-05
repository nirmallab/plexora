"""Magic select's model service: status, the one-time setup, inference.

Nothing here needs ONNX Runtime or the real weights: the download is served
from a local HTTP server with tiny stand-in files (their checksums pinned for
the test), and inference runs on `FakeSamBackend`.
"""

import hashlib
import http.server
import threading
import time

import numpy as np
import pytest

from plexora.vision import sam, sam_backend, sam_weights


@pytest.fixture
def tiny_release(monkeypatch, tmp_path):
    """Two small 'model files' served over HTTP, pinned as MODEL_FILES."""
    served = tmp_path / "release"
    served.mkdir()
    files = {}
    for name, size in (("mobile_sam_encoder.onnx", 300_000), ("mobile_sam_decoder.onnx", 120_000)):
        data = np.random.default_rng(size).integers(0, 255, size, dtype=np.uint8).tobytes()
        (served / name).write_bytes(data)
        files[name] = {"role": "encoder" if "encoder" in name else "decoder",
                       "sha256": hashlib.sha256(data).hexdigest(), "bytes": size}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(served), **kwargs)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(sam_weights, "MODEL_FILES", files)
    monkeypatch.setattr(sam_weights, "TOTAL_BYTES", sum(f["bytes"] for f in files.values()))
    monkeypatch.setenv(sam_weights.ENV_MODEL_URL, f"http://127.0.0.1:{server.server_port}/")
    monkeypatch.setenv("PLEXORA_SEGMENT_MODEL_DIR", str(tmp_path / "models"))
    yield served, files
    server.shutdown()


def _wait_for(predicate, timeout=20.0):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_status_says_what_is_missing_and_never_imports_the_runtime(monkeypatch, tmp_path):
    import sys

    monkeypatch.setenv("PLEXORA_SEGMENT_MODEL_DIR", str(tmp_path / "nothing"))
    status = sam.status().to_dict()
    assert status["state"] in ("weights_missing", "not_installed_runtime")
    assert set(status["weights"]["missing"]) == set(sam_weights.MODEL_FILES)
    assert status["weights"]["source"] == "environment"
    assert status["model"] == {"name": "mobile_sam", "version": sam_weights.MODEL_VERSION,
                               "license": "Apache-2.0"}
    assert status["ready"] is False and status["size_mb"] > 0
    assert not sam.available()
    # Cheap by contract: a polling page must not pay for the runtime's import.
    if "onnxruntime" not in sys.modules:
        sam.status()
        assert "onnxruntime" not in sys.modules


def test_the_switch_turns_everything_off(monkeypatch):
    monkeypatch.setenv(sam.ENV_SWITCH, "0")
    assert sam.status().state == "disabled"
    assert not sam.available()
    with sam.use_backend(sam_backend.FakeSamBackend()):
        assert not sam.available()


def test_the_default_directory_is_under_the_data_root(monkeypatch):
    from plexora import paths

    monkeypatch.delenv("PLEXORA_SEGMENT_MODEL_DIR", raising=False)
    directory, source = paths.segment_model_dir()
    assert source == "default"
    assert directory == paths.data_root() / ".models" / "segment"


def test_setup_downloads_verifies_and_is_idempotent(tiny_release):
    _served, files = tiny_release
    assert sam.status().state in ("weights_missing", "not_installed_runtime")
    first = sam.start_install()
    second = sam.start_install()          # every tab may ask; one download
    assert first.state in ("downloading", "ready", "not_installed_runtime")
    assert second.download is None or second.download["total"] == sum(
        f["bytes"] for f in files.values())
    assert _wait_for(lambda: not sam.status().download or not sam.status().download["running"])
    after = sam.status()
    assert after.weights["missing"] == []
    assert after.download["done"] == after.download["total"]
    assert after.download["error"] is None
    for name, entry in files.items():
        assert sam_weights.verify(sam_weights.model_dir() / name, entry["sha256"])
    assert sam.start_install().weights["missing"] == []   # nothing to do


def test_a_corrupt_download_never_gets_its_real_name(tiny_release):
    served, files = tiny_release
    (served / "mobile_sam_decoder.onnx").write_bytes(b"x" * files["mobile_sam_decoder.onnx"]["bytes"])
    with pytest.raises(sam_weights.WeightsCorrupt):
        sam_weights.download()
    directory = sam_weights.model_dir()
    assert not (directory / "mobile_sam_decoder.onnx").exists()
    assert not (directory / "mobile_sam_decoder.onnx.part").exists()
    # The background setup reports it as an error state, with the reason.
    sam.start_install()
    assert _wait_for(lambda: not sam.status().download["running"])
    status = sam.status()
    if status.state != "not_installed_runtime":
        assert status.state == "error" and "checksum" in status.hint


def test_a_cancelled_download_leaves_nothing_half_written(tiny_release):
    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) > 1

    with pytest.raises(sam_weights.DownloadCancelled):
        sam_weights.download(cancelled=cancelled)
    leftovers = list(sam_weights.model_dir().glob("*.part"))
    assert leftovers == []


def test_remove_deletes_the_files(tiny_release):
    sam_weights.download()
    gone = sam_weights.remove()
    assert set(gone) >= set(sam_weights.MODEL_FILES)
    assert sam_weights.missing() == list(sam_weights.MODEL_FILES)


def test_preprocessing_resizes_the_long_side_normalises_and_zero_pads():
    rgb = np.full((300, 600, 3), 200, np.uint8)
    out = np.empty((1, 3, 1024, 1024), np.float32)
    nh, nw, scale = sam.preprocess_into(rgb, out)
    assert (nh, nw) == (512, 1024) and scale == pytest.approx(1024 / 600)
    expected = (200 - sam._MEAN) * sam._INV_STD
    assert np.allclose(out[0, :, :nh, :nw].mean(axis=(1, 2)), expected, atol=1e-4)
    assert not out[0, :, nh:, :].any()


def test_prompts_are_scaled_into_the_model_frame_and_padded_per_set():
    embedding = sam.Embedding(array=np.zeros((1, 256, 64, 64), np.float32), scale=2.0,
                              crop_hw=(256, 512), input_hw=(512, 1024))
    coords, labels = sam._prompt_arrays(embedding, [
        ([[10, 20]], [1], None),
        ([[10, 20], [30, 40]], [1, 0], (0, 0, 100, 50)),
    ])
    assert coords.shape == (2, 4, 2) and labels.shape == (2, 4)
    assert coords[0, 0].tolist() == [20.0, 40.0]
    assert labels[0].tolist() == [1, -1, -1, -1]           # padding point, then padding
    assert labels[1].tolist() == [1, 0, 2, 3]               # box corners are 2 and 3
    assert coords[1, 3].tolist() == [200.0, 100.0]


def test_one_point_takes_the_best_scored_candidate_that_is_not_the_whole_field():
    iou = np.array([[0.5, 0.6, 0.9, 0.99]], np.float32)
    low = np.full((1, 4, 256, 256), -1.0, np.float32)
    low[0, 3] = 1.0                                         # output 3 covers everything
    low[0, 2, :20, :20] = 1.0
    picked = sam.choose(iou, low, multimask=True, max_fraction=0.9, input_hw=(1024, 1024))
    assert picked.tolist() == [2]
    assert sam.choose(iou, low, multimask=False).tolist() == [0]


def test_predict_batches_prompt_sets_through_one_decoder_call():
    fake = sam_backend.FakeSamBackend()
    image = np.zeros((400, 400, 3), np.uint8)
    image[100:200, 100:200] = 220
    image[300:350, 300:350] = 220
    with sam.use_backend(fake):
        embedding = sam.embed(image)
        results = sam.predict_batch(embedding, [([[150, 150]], [1], None),
                                                ([[325, 325]], [1], None)])
    assert [c[0] for c in fake.calls] == ["embed", "decode"]
    assert results[0].mask[150, 150] and not results[0].mask[325, 325]
    assert results[1].mask[325, 325] and not results[1].mask[150, 150]
    square = results[0].mask[90:210, 90:210].sum()
    assert 0.8 * 100 * 100 <= square <= 1.25 * 100 * 100
    assert results[0].low_res.shape == (1, 256, 256)


def test_a_point_on_glass_is_a_small_disc_and_negatives_carve():
    fake = sam_backend.FakeSamBackend()
    image = np.zeros((256, 256, 3), np.uint8)
    image[50:200, 50:200] = 200
    with sam.use_backend(fake):
        embedding = sam.embed(image)
        whole = sam.predict(embedding, [[120, 120]], [1])
        carved = sam.predict(embedding, [[120, 120], [60, 60]], [1, 0])
    assert whole.mask[60, 60] and not carved.mask[60, 60]
    assert carved.mask[120, 120]


def test_the_backend_picks_providers_and_falls_back_to_cpu(monkeypatch):
    class FakeSession:
        built = []

        def __init__(self, path, options, providers):
            first = providers[0][0] if isinstance(providers[0], tuple) else providers[0]
            if first == "CUDAExecutionProvider":
                raise RuntimeError("no CUDA driver")
            FakeSession.built.append(first)
            self._providers = [first]

        def get_providers(self):
            return self._providers

    class FakeOptions:
        def add_session_config_entry(self, *args):
            pass

    class FakeOrt:
        __version__ = "test"
        InferenceSession = FakeSession
        SessionOptions = FakeOptions

        class GraphOptimizationLevel:
            ORT_ENABLE_ALL = 99

        class ExecutionMode:
            ORT_SEQUENTIAL = 0

        @staticmethod
        def get_available_providers():
            return ["CUDAExecutionProvider", "CoreMLExecutionProvider", "CPUExecutionProvider"]

    monkeypatch.setattr(sam_backend, "import_runtime", lambda: FakeOrt)
    backend = sam_backend.OnnxSamBackend("enc.onnx", "dec.onnx", device="auto")
    # CUDA failed to build, so the encoder went to the next GPU (CoreML) and the
    # decoder -- which CoreML handles badly -- to the CPU.
    assert backend.device["encoder"] == "CoreMLExecutionProvider"
    assert backend.device["decoder"] == "CPUExecutionProvider"
    assert backend.device["gpu"] is True
    assert "CUDAExecutionProvider failed" in backend.device["fallback"]
    cpu = sam_backend.OnnxSamBackend("enc.onnx", "dec.onnx", device="cpu")
    assert cpu.device["encoder"] == cpu.device["decoder"] == "CPUExecutionProvider"
    assert cpu.device["gpu"] is False
