"""Magic select through a z-stack: SAM 2's video predictor, planes as frames.

MobileSAM (plexora/vision/sam.py) outlines one picture. Carrying an outline
from plane to plane is what SAM 2 was built for: its memory of the frames it
has seen keeps the same object as the object changes shape, and its
object-presence head says when the object has ended -- the poles of a nucleus,
where a model prompted plane by plane keeps finding *something*. Measured on
synthetic nuclei and on real stacks before this was chosen (docs/internal/3d).

Optional. PyTorch and the `sam2` package come with the `volume3d` extra
(`pip install "plexora[volume3d]"`); without them `available()` is False and
plexora/vision/propagate.py carries outlines with MobileSAM instead, plane by
plane. Nothing here imports torch until a stack is actually carried.

The weights are Meta's SAM 2.1 release (Apache-2.0), fetched once into
`<model dir>/sam2/` and checked against the sha256 pinned below, exactly as
MobileSAM's are (`sam_weights.fetch`).

The predictor reads frames from a video file or a folder of JPEGs. Planes are
handed to it from memory instead (`_frames_from_memory`): the same tensor its
loader would have built -- RGB in 0..1, resized to the model's square input,
ImageNet-normalised -- without writing a lossy JPEG per plane.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from plexora.vision import sam_weights

EXTRA = "volume3d"
INSTALL = f'pip install "plexora[{EXTRA}]"'

MODEL_NAME = "sam2.1_hiera_tiny"
MODEL_VERSION = "1"
MODEL_LICENSE = "Apache-2.0"
MODEL_CONFIG = "configs/sam2.1/sam2.1_hiera_t.yaml"
RELEASE_BASE = "https://dl.fbaipublicfiles.com/segment_anything_2/092824/"
ENV_MODEL_URL = "PLEXORA_SEGMENT3D_MODEL_URL"
MODEL_FILES = {
    "sam2.1_hiera_tiny.pt": {
        "sha256": "7402e0d864fa82708a20fbd15bc84245c2f26dff0eb43a4b5b93452deb34be69",
        "bytes": 156008466,
    },
}

#: At most this many planes are held as frames for one carry (each is a
#: 3 x 1024 x 1024 float tensor on the CPU, 12 MB).
MAX_FRAMES = 96

_BUILD_LOCK = threading.Lock()
_INFER = threading.Lock()
_PATCH = threading.Lock()
_PREDICTOR = None
_DEVICE: str | None = None
_DOWNLOAD = {"thread": None, "done": 0, "total": 0, "error": None, "cancel": None}


class Sam2Missing(RuntimeError):
    """The optional runtime or its weights are not here; `INSTALL` says how."""


def runtime_installed() -> bool:
    """torch and sam2 importable -- a `find_spec`, never an import."""
    try:
        return (importlib.util.find_spec("torch") is not None
                and importlib.util.find_spec("sam2") is not None)
    except (ImportError, ValueError):
        return False


def weights_dir() -> Path:
    return sam_weights.model_dir() / "sam2"


def release_base() -> str:
    raw = (os.environ.get(ENV_MODEL_URL) or "").strip()
    base = raw or RELEASE_BASE
    return base if base.endswith("/") else base + "/"


def weights_missing() -> list[str]:
    directory = weights_dir()
    out = []
    for name, entry in MODEL_FILES.items():
        try:
            ok = (directory / name).stat().st_size == entry["bytes"]
        except OSError:
            ok = False
        if not ok:
            out.append(name)
    return out


def enabled() -> bool:
    return os.environ.get("PLEXORA_SEGMENT", "1") != "0"


@dataclass
class Status:
    state: str          # disabled | not_installed_runtime | weights_missing | downloading | error | ready
    ready: bool
    message: str
    progress: float | None = None

    def to_dict(self) -> dict:
        return {"state": self.state, "ready": self.ready, "message": self.message,
                "progress": self.progress, "install": INSTALL,
                "model": {"name": MODEL_NAME, "version": MODEL_VERSION,
                          "license": MODEL_LICENSE},
                "size_mb": round(sum(f["bytes"] for f in MODEL_FILES.values()) / 2**20)}


def status() -> Status:
    """Cheap: a find_spec and a stat, never torch."""
    if not enabled():
        return Status("disabled", False, "Magic select is turned off on this server.")
    if not runtime_installed():
        return Status("not_installed_runtime", False,
                      "Carrying an outline through planes needs the 3-D extra: "
                      + INSTALL)
    thread = _DOWNLOAD["thread"]
    if thread is not None and thread.is_alive():
        total = _DOWNLOAD["total"] or 1
        return Status("downloading", False, "Downloading the 3-D model.",
                      progress=round(_DOWNLOAD["done"] / total, 3))
    if _DOWNLOAD["error"]:
        return Status("error", False, f"The 3-D model could not be set up: {_DOWNLOAD['error']}")
    if weights_missing():
        return Status("weights_missing", False,
                      "The 3-D model is downloaded the first time it is used.")
    return Status("ready", True, "Ready.")


def available() -> bool:
    return status().ready


def start_install() -> Status:
    """Download the weights in the background (idempotent)."""
    if not runtime_installed():
        return status()
    with _BUILD_LOCK:
        thread = _DOWNLOAD["thread"]
        if (thread is None or not thread.is_alive()) and weights_missing():
            cancel = threading.Event()
            _DOWNLOAD.update(done=0, total=0, error=None, cancel=cancel)

            def run():
                try:
                    sam_weights.fetch(
                        MODEL_FILES, release_base(), weights_dir(),
                        progress=lambda done, total, _name: _DOWNLOAD.update(done=done,
                                                                             total=total),
                        cancelled=cancel.is_set)
                except Exception as exc:  # noqa: BLE001 -- said in status, not raised
                    _DOWNLOAD["error"] = str(exc) or type(exc).__name__

            thread = threading.Thread(target=run, name="segment3d-download", daemon=True)
            _DOWNLOAD["thread"] = thread
            thread.start()
    return status()


def ensure_installed(progress=None) -> None:
    """Blocking download (the CLI, tests with a mirror)."""
    sam_weights.fetch(MODEL_FILES, release_base(), weights_dir(), progress=progress)


def device() -> str:
    """cuda when there is one, else the CPU -- or PLEXORA_SEGMENT_DEVICE.

    Never Apple's GPU (MPS), although it is there and SAM 2 runs on it: the
    prompted plane comes back right, and every plane carried from it wrong --
    masks over the whole frame, or somewhere else entirely (measured on
    synthetic nuclei, 2026-10-10: plane IoU 0.86 on the prompt, 0.00 on every
    other plane; the same frames on the CPU track at 0.84). The model's memory
    of earlier frames is what breaks there. PLEXORA_SEGMENT_DEVICE=mps forces
    it, for checking a newer PyTorch.
    """
    wanted = (os.environ.get("PLEXORA_SEGMENT_DEVICE") or "auto").strip().lower()
    import torch

    if wanted in ("cpu", "mps"):
        return wanted
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def predictor():
    """The video predictor, built once (torch is imported here and nowhere else)."""
    global _PREDICTOR, _DEVICE
    if _PREDICTOR is not None:
        return _PREDICTOR
    with _BUILD_LOCK:
        if _PREDICTOR is not None:
            return _PREDICTOR
        if not runtime_installed():
            raise Sam2Missing("Carrying an outline through planes needs the 3-D extra: "
                              + INSTALL)
        missing = weights_missing()
        if missing:
            raise Sam2Missing("The 3-D model is not downloaded yet (" + ", ".join(missing)
                              + ").")
        # Before torch is imported: ops Apple's GPU lacks run on the CPU.
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        from sam2.build_sam import build_sam2_video_predictor

        name = next(iter(MODEL_FILES))
        path = weights_dir() / name
        if not sam_weights.verify(path, MODEL_FILES[name]["sha256"]):
            raise Sam2Missing(f"{name} does not match its checksum; delete it and retry.")
        _DEVICE = device()
        _PREDICTOR = build_sam2_video_predictor(MODEL_CONFIG, str(path), device=_DEVICE)
    return _PREDICTOR


def used_device() -> str | None:
    return _DEVICE


@contextmanager
def _frames_from_memory(frames):
    """Make `init_state` take `frames` (H x W x 3 uint8) instead of a path."""
    import torch
    import sam2.sam2_video_predictor as video_module

    def load(video_path, image_size, offload_video_to_cpu, img_mean=(0.485, 0.456, 0.406),
             img_std=(0.229, 0.224, 0.225), async_loading_frames=False,
             compute_device=None):
        mean = torch.tensor(img_mean, dtype=torch.float32)[:, None, None]
        std = torch.tensor(img_std, dtype=torch.float32)[:, None, None]
        images = torch.empty(len(frames), 3, image_size, image_size, dtype=torch.float32)
        for i, rgb in enumerate(frames):
            tensor = torch.from_numpy(np.ascontiguousarray(rgb)).permute(2, 0, 1)
            tensor = tensor.float()[None] / 255.0
            images[i] = torch.nn.functional.interpolate(
                tensor, size=(image_size, image_size), mode="bilinear",
                align_corners=False)[0]
        images = (images - mean) / std
        if not offload_video_to_cpu and compute_device is not None:
            images = images.to(compute_device)
        height, width = frames[0].shape[:2]
        return images, height, width

    with _PATCH:
        original = video_module.load_video_frames
        video_module.load_video_frames = load
        try:
            yield
        finally:
            video_module.load_video_frames = original


def carry(frames, start: int, *, box=None, points=None, labels=None, mask=None,
          forward: bool = True, backward: bool = True, until=None):
    """Masks for the object prompted on `frames[start]`, carried both ways.

    `box` (x0, y0, x1, y1), `points`/`labels` and `mask` (H x W bool) are in
    the frames' pixels. Returns `{index: (mask, score)}`: `score` is the
    model's object-presence logit for that frame (below 0: not there).

    `until(index, mask, score)` is asked after each frame (the prompted one
    first, in each direction); False ends that direction there -- the frames
    past an object's end are never computed, which is most of the cost of a
    carry through a deep stack.
    """
    import torch

    if not frames:
        return {}
    if len(frames) > MAX_FRAMES:
        raise ValueError(f"at most {MAX_FRAMES} planes at once")
    model = predictor()
    out = {}
    with _INFER, torch.inference_mode(), _frames_from_memory(frames):
        state = model.init_state(video_path="<planes>", offload_video_to_cpu=True,
                                 offload_state_to_cpu=_DEVICE != "cuda")
        try:
            if mask is not None:
                model.add_new_mask(state, frame_idx=start, obj_id=1,
                                   mask=np.asarray(mask, dtype=bool))
            else:
                model.add_new_points_or_box(
                    state, frame_idx=start, obj_id=1,
                    points=None if points is None else np.asarray(points, np.float32),
                    labels=None if labels is None else np.asarray(labels, np.int32),
                    box=None if box is None else np.asarray(box, np.float32))
            directions = [False] * forward + [True] * backward
            for reverse in directions:
                for index, _ids, logits in model.propagate_in_video(
                        state, start_frame_idx=start, reverse=reverse):
                    values = logits[0, 0]
                    got = (np.asarray((values > 0).cpu().numpy()), float(values.max().item()))
                    out[int(index)] = got
                    if until is not None and not until(int(index), *got):
                        break
        finally:
            model.reset_state(state)
    return out
