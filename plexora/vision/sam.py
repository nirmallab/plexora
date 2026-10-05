"""Magic select's model: status, one-time setup, and prompted segmentation.

Everything here works on an RGB crop the caller has already composited --
`plexora.vision.segment` turns a project view into one -- and answers in that
crop's pixel frame. Nothing here knows about projects, ROIs or QC.

Three rules the callers depend on:

- **`status()` is cheap.** A `find_spec` and a few stats; it never imports
  ONNX Runtime and never hashes a file, because a downloading page polls it
  once a second.
- **One inference at a time** (`_INFER`). Embedding and decoding share the
  CPU (or the GPU) with tile serving; two embeddings racing would make both
  slow. `QUEUE` bounds how many HTTP callers may wait for it; the route
  answers 429 past that.
- **Setup is idempotent.** `start_install()` may be called by every tab that
  noticed `weights_missing`; there is only ever one download thread.

`PLEXORA_SEGMENT=0` makes every caller see "not available" (the bench and
locked-down machines use it): `available()` is False, setup refuses, and the
QC tracer uses its classical methods.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import threading
import time
from dataclasses import asdict, dataclass, field

import numpy as np

from plexora.vision import sam_backend, sam_weights

ENV_SWITCH = "PLEXORA_SEGMENT"

STATES = ("disabled", "not_installed_runtime", "weights_missing", "downloading",
          "error", "ready")

MAX_POINTS = 32
MAX_AGENT_REFINEMENTS = 3
INPUT_SIDE = sam_backend.INPUT_SIDE
LOW_RES = sam_backend.LOW_RES

# ImageNet statistics in 0..255 units, as SAM was trained.
_MEAN = np.array([123.675, 116.28, 103.53], np.float32)
_INV_STD = (1.0 / np.array([58.395, 57.12, 57.375], np.float32)).astype(np.float32)

_INFER = threading.Lock()
QUEUE = threading.BoundedSemaphore(4)

_SESSION_LOCK = threading.Lock()
_BACKEND = None
_OVERRIDE = None
_SESSION_ERROR: str | None = None
_WARMING: threading.Thread | None = None

_SCRATCH = None
_SCRATCH_LOCK = threading.Lock()


# -- status ------------------------------------------------------------------


def enabled() -> bool:
    raw = (os.environ.get(ENV_SWITCH) or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def runtime_installed() -> bool:
    return importlib.util.find_spec("onnxruntime") is not None


def _runtime_version() -> str | None:
    import sys

    module = sys.modules.get("onnxruntime")
    return getattr(module, "__version__", None) if module else None


@dataclass
class _Download:
    running: bool = False
    done: int = 0
    total: int = sam_weights.TOTAL_BYTES
    file: str | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    directory: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict:
        out = {key: getattr(self, key) for key in ("running", "done", "total", "file",
                                                    "error", "started_at", "finished_at")}
        out["fraction"] = round(self.done / self.total, 4) if self.total else 0.0
        return out


_DOWNLOAD = _Download()
_DOWNLOAD_LOCK = threading.Lock()
_DOWNLOAD_THREAD: threading.Thread | None = None


@dataclass
class Status:
    state: str
    runtime: dict
    weights: dict
    download: dict | None
    model: dict
    device: dict | None
    hint: str | None

    def to_dict(self) -> dict:
        out = asdict(self)
        out["ready"] = self.state == "ready"
        out["size_mb"] = round(sam_weights.TOTAL_BYTES / 1e6)
        return out


def status() -> Status:
    directory, source = sam_weights.model_dir(), sam_weights.model_dir_source()
    files = sam_weights.file_status(directory)
    missing = [name for name, row in files.items() if not row["ok"]]
    installed = runtime_installed()
    with _DOWNLOAD_LOCK:
        # A setup run for another directory (the setting changed since) says
        # nothing about this one.
        mine = _DOWNLOAD.directory in (None, str(directory))
        download = _DOWNLOAD.public() if (_DOWNLOAD.started_at is not None and mine) else None
        running = _DOWNLOAD.running and mine
        failed = _DOWNLOAD.error if (not running and mine) else None
    backend = _OVERRIDE or _BACKEND
    if not enabled():
        state, hint = "disabled", f"{ENV_SWITCH}=0 switches magic select off."
    elif not installed and _OVERRIDE is None:
        state, hint = "not_installed_runtime", sam_backend.SamRuntimeMissing.INSTALL
    elif running:
        state, hint = "downloading", None
    elif missing and _OVERRIDE is None:
        if failed:
            state, hint = "error", failed
        else:
            state, hint = "weights_missing", sam_weights.SamWeightsMissing.INSTALL
    elif _SESSION_ERROR and backend is None:
        state, hint = "error", _SESSION_ERROR
    else:
        state, hint = "ready", None
    return Status(
        state=state,
        runtime={"installed": installed, "version": _runtime_version(),
                 "install": sam_backend.SamRuntimeMissing.INSTALL},
        weights={"dir": str(directory), "source": source, "files": files,
                 "missing": missing},
        download=download,
        model={"name": sam_weights.MODEL_NAME, "version": sam_weights.MODEL_VERSION,
               "license": sam_weights.MODEL_LICENSE},
        device=dict(backend.device) if backend is not None else None,
        hint=hint,
    )


def available() -> bool:
    """Ready to segment now (or a test backend is installed)."""
    if not enabled():
        return False
    if _OVERRIDE is not None:
        return True
    return status().state == "ready"


# -- setup -------------------------------------------------------------------


def start_install() -> Status:
    """Begin the one-time download on a daemon thread; idempotent."""
    global _DOWNLOAD_THREAD
    if not enabled():
        return status()
    with _DOWNLOAD_LOCK:
        running = _DOWNLOAD.running
    if running or not sam_weights.missing():
        return status()
    directory = sam_weights.model_dir()
    if not sam_weights._writable(directory):
        raise PermissionError(f"Cannot write to {directory}")
    with _DOWNLOAD_LOCK:
        # Checked again under the lock: two tabs may get here together, and
        # only one of them starts a thread. status() takes this lock too, so
        # it is called after the block, never inside it.
        if not _DOWNLOAD.running:
            _DOWNLOAD.running = True
            _DOWNLOAD.done = 0
            _DOWNLOAD.total = sam_weights.TOTAL_BYTES
            _DOWNLOAD.directory = str(directory)
            _DOWNLOAD.error = None
            _DOWNLOAD.file = None
            _DOWNLOAD.started_at = time.time()
            _DOWNLOAD.finished_at = None
            _DOWNLOAD.cancel = threading.Event()
            _DOWNLOAD_THREAD = threading.Thread(
                target=_install_thread, args=(directory, _DOWNLOAD.cancel),
                name="plexora-segment-setup", daemon=True)
            _DOWNLOAD_THREAD.start()
    return status()


def _install_thread(directory, cancel):
    def progress(done, total, name):
        with _DOWNLOAD_LOCK:
            _DOWNLOAD.done, _DOWNLOAD.total, _DOWNLOAD.file = done, total, name

    error = None
    try:
        sam_weights.download(directory, progress=progress, cancelled=cancel.is_set)
    except sam_weights.DownloadCancelled:
        error = None
    except Exception as exc:  # network, disk, checksum
        error = _readable(exc)
    with _DOWNLOAD_LOCK:
        _DOWNLOAD.running = False
        _DOWNLOAD.error = error
        _DOWNLOAD.finished_at = time.time()
    if error is None and not cancel.is_set():
        warm_async()


def _readable(exc) -> str:
    text = str(exc) or type(exc).__name__
    return text[:300]


def cancel_install() -> Status:
    with _DOWNLOAD_LOCK:
        if _DOWNLOAD.running:
            _DOWNLOAD.cancel.set()
    thread = _DOWNLOAD_THREAD
    if thread is not None:
        thread.join(timeout=5)
    return status()


def ensure_installed(progress=None, force=False):
    """Synchronous setup for the CLI. Returns the model directory."""
    if not enabled():
        raise RuntimeError(f"{ENV_SWITCH}=0 switches magic select off.")
    return sam_weights.download(progress=progress, force=force)


def remove() -> list[str]:
    global _BACKEND
    with _SESSION_LOCK:
        _BACKEND = None
    return sam_weights.remove()


# -- session -----------------------------------------------------------------


def _build_backend():
    sam_backend.import_runtime()
    directory = sam_weights.model_dir()
    missing = sam_weights.missing(directory, deep=True)
    if missing:
        raise sam_weights.SamWeightsMissing(missing)
    return sam_backend.OnnxSamBackend(
        sam_weights.path_for("encoder", directory),
        sam_weights.path_for("decoder", directory),
        cache_dir=directory / "cache")


def session():
    """The backend, built once. Raises SamRuntimeMissing / SamWeightsMissing."""
    global _BACKEND, _SESSION_ERROR
    if _OVERRIDE is not None:
        return _OVERRIDE
    backend = _BACKEND
    if backend is not None:
        return backend
    with _SESSION_LOCK:
        if _BACKEND is None:
            try:
                _BACKEND = _build_backend()
                _SESSION_ERROR = None
            except (sam_backend.SamRuntimeMissing, sam_weights.SamWeightsMissing):
                raise
            except Exception as exc:
                _SESSION_ERROR = _readable(exc)
                raise
        return _BACKEND


def warm_async():
    """Build the session on a background thread (CoreML compiles for seconds)."""
    global _WARMING
    if not enabled() or _BACKEND is not None or _OVERRIDE is not None:
        return None
    if _WARMING is not None and _WARMING.is_alive():
        return _WARMING

    def run():
        try:
            backend = session()
            # One embed so the accelerator's first-run cost is paid now.
            with _INFER:
                backend.embed(np.zeros((1, 3, INPUT_SIDE, INPUT_SIDE), np.float32))
        except Exception:
            pass

    _WARMING = threading.Thread(target=run, name="plexora-segment-warm", daemon=True)
    _WARMING.start()
    return _WARMING


@contextlib.contextmanager
def use_backend(backend):
    """Tests: route every call to `backend` (e.g. FakeSamBackend)."""
    global _OVERRIDE
    previous = _OVERRIDE
    _OVERRIDE = backend
    try:
        yield backend
    finally:
        _OVERRIDE = previous


def prime(log=None):
    """`prime_hot_code`'s step: import the runtime now, warm the session later.

    The import (and its dlopen of the native library) happens on the priming
    thread, before any request exists -- the loader-lock rule
    `data_model.prime_hot_code` describes. Building the session can take
    seconds on a GPU, so it is started in the background and only when the
    weights are already on disk.
    """
    if not enabled() or not runtime_installed():
        return
    import cv2  # noqa: F401 -- preprocessing and polygons

    sam_backend.import_runtime()
    if not sam_weights.missing():
        warm_async()


# -- inference ---------------------------------------------------------------


@dataclass
class Embedding:
    array: np.ndarray          # 1x256x64x64 float32
    scale: float               # crop px -> model px
    crop_hw: tuple[int, int]
    input_hw: tuple[int, int]  # the resized crop inside the 1024 square


@dataclass
class Prediction:
    mask: np.ndarray           # bool, crop_hw
    iou: float
    low_res: np.ndarray        # 1x256x256 float32 logits, for the next round
    index: int


def _scratch() -> np.ndarray:
    global _SCRATCH
    if _SCRATCH is None:
        _SCRATCH = np.zeros((1, 3, INPUT_SIDE, INPUT_SIDE), np.float32)
    return _SCRATCH


def input_size(crop_hw) -> tuple[int, int, float]:
    h, w = int(crop_hw[0]), int(crop_hw[1])
    scale = INPUT_SIDE / max(h, w)
    return int(h * scale + 0.5), int(w * scale + 0.5), scale


def preprocess_into(rgb: np.ndarray, out: np.ndarray):
    """Resize the longest side to 1024, normalise, zero-pad -- one pass."""
    import cv2

    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("expected an H x W x 3 image")
    nh, nw, scale = input_size(rgb.shape[:2])
    if (nh, nw) != rgb.shape[:2]:
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        resized = cv2.resize(np.ascontiguousarray(rgb), (nw, nh), interpolation=interp)
    else:
        resized = rgb
    out.fill(0.0)
    view = out[0, :, :nh, :nw]
    np.subtract(resized.transpose(2, 0, 1), _MEAN[:, None, None], out=view,
                casting="unsafe")
    view *= _INV_STD[:, None, None]
    return nh, nw, scale


def embed(rgb_u8: np.ndarray) -> Embedding:
    """Run the image encoder on an H x W x 3 uint8 crop (long side <= 1024 best)."""
    backend = session()
    with _INFER:
        with _SCRATCH_LOCK:
            buffer = _scratch()
            nh, nw, scale = preprocess_into(rgb_u8, buffer)
            array = backend.embed(buffer)
    return Embedding(array=np.asarray(array, np.float32), scale=scale,
                     crop_hw=(int(rgb_u8.shape[0]), int(rgb_u8.shape[1])),
                     input_hw=(nh, nw))


def _prompt_arrays(embedding: Embedding, prompts):
    """B prompt sets -> padded (B, N, 2) coords and (B, N) labels, vectorised.

    Each prompt set is `(points_xy (k,2), labels (k,), box (4,) | None)` in crop
    pixels. A box becomes two corner points labelled 2 and 3; a set without a
    box gets SAM's padding point (label -1). Shorter sets are padded with -1.
    """
    rows = []
    for points, labels, box in prompts:
        pts = np.asarray(points, np.float32).reshape(-1, 2)
        lab = np.asarray(labels, np.float32).reshape(-1)
        if box is not None:
            x0, y0, x1, y1 = (float(v) for v in box)
            pts = np.concatenate([pts, np.array([[x0, y0], [x1, y1]], np.float32)])
            lab = np.concatenate([lab, np.array([2, 3], np.float32)])
        else:
            pts = np.concatenate([pts, np.zeros((1, 2), np.float32)])
            lab = np.concatenate([lab, np.array([-1], np.float32)])
        rows.append((pts, lab))
    width = max(len(lab) for _, lab in rows)
    coords = np.zeros((len(rows), width, 2), np.float32)
    labels = np.full((len(rows), width), -1, np.float32)
    for i, (pts, lab) in enumerate(rows):
        coords[i, : len(lab)] = pts
        labels[i, : len(lab)] = lab
    h, w = embedding.crop_hw
    nh, nw = embedding.input_hw
    coords *= np.array([nw / w, nh / h], np.float32)
    return coords, labels


def upsample(embedding: Embedding, low_res: np.ndarray) -> np.ndarray:
    """256x256 logits -> crop-sized logits, exactly as SAM post-processes."""
    import cv2

    nh, nw = embedding.input_hw
    h, w = embedding.crop_hw
    full = cv2.resize(low_res.astype(np.float32, copy=False), (INPUT_SIDE, INPUT_SIDE),
                      interpolation=cv2.INTER_LINEAR)
    return cv2.resize(full[:nh, :nw], (w, h), interpolation=cv2.INTER_LINEAR)


def choose(iou: np.ndarray, low_res: np.ndarray, *, multimask: bool,
           max_fraction: float | None = None, input_hw=None) -> np.ndarray:
    """Pick one output per prompt set, vectorised over the batch.

    Single-mask prompts (a box, two or more points, a mask from the previous
    round) take output 0, which SAM trains for exactly that. One point is
    ambiguous -- a cell, a cluster, the whole tissue -- so the best scoring of
    outputs 1..3 is taken, after dropping candidates that cover more than
    `max_fraction` of the crop (the "whole field" answer an artifact never is).
    """
    batch = iou.shape[0]
    if not multimask:
        return np.zeros(batch, np.int64)
    score = iou[:, 1:].astype(np.float64).copy()
    if max_fraction is not None and input_hw is not None:
        qh = max(1, int(np.ceil(input_hw[0] / 4)))
        qw = max(1, int(np.ceil(input_hw[1] / 4)))
        area = (low_res[:, 1:, :qh, :qw] > 0).mean(axis=(2, 3))
        score[area > max_fraction] -= 10.0
    return 1 + np.argmax(score, axis=1)


def predict_batch(embedding: Embedding, prompts, *, mask_input=None,
                  max_fraction: float | None = 0.9) -> list[Prediction]:
    """Decode B prompt sets against one embedding in a single call."""
    if not prompts:
        return []
    coords, labels = _prompt_arrays(embedding, prompts)
    batch = coords.shape[0]
    if mask_input is not None:
        masks = np.asarray(mask_input, np.float32).reshape(-1, 1, LOW_RES, LOW_RES)
        if masks.shape[0] != batch:
            masks = np.broadcast_to(masks[:1], (batch, 1, LOW_RES, LOW_RES))
        has = np.ones(1, np.float32)
    else:
        masks = np.zeros((batch, 1, LOW_RES, LOW_RES), np.float32)
        has = np.zeros(1, np.float32)
    backend = session()
    with _INFER:
        iou, low = backend.decode(embedding.array, coords, labels, masks, has)
    iou = np.asarray(iou, np.float32)
    low = np.asarray(low, np.float32)
    singles = np.array([
        box is not None or (np.asarray(lab) >= 0).sum() >= 2 or mask_input is not None
        for _points, lab, box in prompts])
    picks = np.where(singles, 0, choose(iou, low, multimask=True,
                                        max_fraction=max_fraction,
                                        input_hw=embedding.input_hw))
    out = []
    for b in range(batch):
        index = int(picks[b])
        logits = upsample(embedding, low[b, index])
        out.append(Prediction(mask=logits > 0, iou=float(iou[b, index]),
                              low_res=low[b, index][None].copy(), index=index))
    return out


def predict(embedding: Embedding, points_xy, labels, *, box=None, mask_input=None,
            max_fraction: float | None = 0.9) -> Prediction:
    points = np.asarray(points_xy, np.float32).reshape(-1, 2)
    labels = np.asarray(labels, np.float32).reshape(-1)
    if len(points) > MAX_POINTS:
        raise ValueError(f"at most {MAX_POINTS} points")
    return predict_batch(embedding, [(points, labels, box)], mask_input=mask_input,
                         max_fraction=max_fraction)[0]
