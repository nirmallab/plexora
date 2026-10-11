"""Cellpose in 3-D: whole cells through a z-stack, block by block.

Cellpose (Stringer et al.; Cellpose-SAM, Pachitariu et al. 2025) segments a
volume either in 3-D at once (`do_3D`: flows predicted on the XY, XZ and YZ
slices and followed through the volume, which needs the stack's anisotropy --
the z step over the pixel size) or plane by plane with the planes' masks
stitched by overlap (`stitch_threshold`). Both are here; 3-D is the default
because stitching under-segments cells stacked along z.

The extra. Cellpose and PyTorch are not in Plexora's core install; they come
with `plexora[segment3d]` and are imported only when a stack is segmented.
Cellpose 4 is required: Cellpose 3, whose `cyto3`/`nuclei` models were the
alternative, pins numpy below 2.1, which Plexora's numpy cannot meet. Cellpose
4's model is Cellpose-SAM, whose weights (about 1.2 GB) Cellpose downloads on
first use from https://huggingface.co/mouseland/cellpose-sam -- they are
distributed there under their own licence, which is the user's to check
before commercial use (`status()` says so; Plexora never bundles them).

Blocks. A whole slide's stack does not fit in one Cellpose call, so the XY
plane is cut into blocks (`BLOCK` px, with `HALO` px of context on every side)
that each run through every plane. A cell belongs to the block whose CORE
(the block without its halo) holds its centroid, and is written whole from
that block -- so a cell cut by a block's edge is written by the neighbour that
sees all of it, never twice, and ids are unique across the stack. A cell
larger than the halo can be cut; the halo is set to a few cell diameters.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import math
import threading
from dataclasses import dataclass

import numpy as np

EXTRA = "segment3d"
INSTALL = 'pip install "plexora[segment3d]"'
WEIGHTS_PAGE = "https://huggingface.co/mouseland/cellpose-sam"
#: The core of a block (px at the level segmented) and its context.
BLOCK = 1024
HALO = 64

_lock = threading.Lock()
_models: dict = {}


class CellposeMissing(RuntimeError):
    """Cellpose is not installed here; `INSTALL` says how (layer_jobs shows it)."""

    INSTALL = INSTALL


def installed() -> bool:
    """Whether Cellpose can be imported, without importing it."""
    return importlib.util.find_spec("cellpose") is not None


def version() -> str | None:
    try:
        return importlib.metadata.version("cellpose")
    except importlib.metadata.PackageNotFoundError:
        return None


def status() -> dict:
    """What segmenting here would use: whether Cellpose is installed, which
    version and model, whether the weights are on this machine yet, and the
    note about their licence. Imports nothing heavy."""
    found = version()
    out = {"installed": bool(found) and installed(), "version": found, "extra": EXTRA,
           "install": INSTALL, "model": "Cellpose-SAM", "weights_page": WEIGHTS_PAGE,
           "licence": ("Cellpose-SAM's weights are downloaded by Cellpose from the Cellpose "
                       "authors' model page and carry their licence (non-commercial at the "
                       "time of writing); check it before commercial use.")}
    if found and int(found.split(".")[0]) < 4:
        out["installed"] = False
        out["problem"] = (f"Cellpose {found} is installed; Cellpose 4 or later is needed "
                          "(Cellpose 3 cannot share Plexora's numpy)")
    if out["installed"]:
        out["weights_cached"] = _weights_cached()
    return out


def _weights_cached() -> bool:
    import os
    from pathlib import Path

    folder = os.environ.get("CELLPOSE_LOCAL_MODELS_PATH")
    folder = Path(folder) if folder else Path.home() / ".cellpose" / "models"
    return any(folder.glob("cpsam*")) if folder.is_dir() else False


def _model(gpu: bool):
    if not installed():
        raise CellposeMissing(f"Segmenting in 3-D needs Cellpose: {INSTALL}")
    with _lock:
        held = _models.get(gpu)
        if held is None:
            from cellpose import models

            held = _models[gpu] = models.CellposeModel(gpu=gpu)
        return held


def use_gpu(device: str) -> bool:
    """`auto`: a CUDA or Apple GPU when PyTorch has one; `cpu`: never."""
    if device == "cpu":
        return False
    try:
        import torch
    except ImportError:
        return False
    if torch.cuda.is_available():
        return True
    return bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())


@dataclass(frozen=True)
class Settings:
    """How to segment, in the units of the level being segmented."""

    mode: str = "3d"                 # "3d" (do_3D) or "stitch" (plane by plane)
    anisotropy: float = 1.0          # z step / pixel size, at this level
    diameter_px: float | None = None
    stitch_threshold: float = 0.4
    cellprob_threshold: float = 0.0
    min_size_vox: int = 15
    gpu: bool = False


def segment_block(image: np.ndarray, settings: Settings) -> np.ndarray:
    """Labels (Z, Y, X) uint32 for `image` (Z, C, Y, X), C = 1 or 2 (nuclear,
    then cytoplasm/membrane)."""
    model = _model(settings.gpu)
    x = np.asarray(image, dtype=np.float32)
    kwargs = dict(z_axis=0, channel_axis=1, cellprob_threshold=settings.cellprob_threshold,
                  min_size=int(settings.min_size_vox), batch_size=8)
    if settings.diameter_px:
        kwargs["diameter"] = float(settings.diameter_px)
    if settings.mode == "3d":
        kwargs.update(do_3D=True, anisotropy=float(settings.anisotropy))
    else:
        kwargs.update(do_3D=False, stitch_threshold=float(settings.stitch_threshold))
    masks = model.eval(x, **kwargs)[0]
    masks = np.asarray(masks)
    if masks.ndim == 2:                  # a one-plane block
        masks = masks[None]
    return masks.astype(np.uint32, copy=False)


def blocks(height: int, width: int, block: int = BLOCK, halo: int = HALO):
    """`(core, padded)` boxes `(y0, y1, x0, x1)` covering the plane: cores
    tile it, each padded by `halo` (clipped to the image)."""
    for y0 in range(0, height, block):
        for x0 in range(0, width, block):
            core = (y0, min(height, y0 + block), x0, min(width, x0 + block))
            padded = (max(0, y0 - halo), min(height, core[1] + halo),
                      max(0, x0 - halo), min(width, core[3] + halo))
            yield core, padded


def place(out: np.ndarray, labels: np.ndarray, core, padded, next_id: int) -> int:
    """Write into `out` (Z, H, W) the cells of one block's `labels` whose
    centroid lies in its `core`, each under a new id; returns the next free id.
    A voxel already written by a neighbour's cell is left to it."""
    present = np.unique(labels)
    present = present[present > 0]
    if not present.size:
        return next_id
    py0, _, px0, _ = padded
    zs, ys, xs = np.nonzero(labels)
    ids = labels[zs, ys, xs]
    order = np.argsort(ids, kind="stable")
    ids, zs, ys, xs = ids[order], zs[order], ys[order], xs[order]
    starts = np.searchsorted(ids, present)
    ends = np.searchsorted(ids, present, side="right")
    cy0, cy1, cx0, cx1 = core
    for start, end in zip(starts, ends):
        cy = py0 + ys[start:end].mean()
        cx = px0 + xs[start:end].mean()
        if not (cy0 <= cy < cy1 and cx0 <= cx < cx1):
            continue
        z, y, x = zs[start:end], py0 + ys[start:end], px0 + xs[start:end]
        free = out[z, y, x] == 0
        out[z[free], y[free], x[free]] = next_id
        next_id += 1
    return next_id


def halo_for(diameter_px: float | None) -> int:
    """Enough context that a cell near a block's edge is whole in the padded
    block: three diameters, at least the default."""
    if not diameter_px:
        return HALO
    return max(HALO, int(math.ceil(3 * float(diameter_px))))
