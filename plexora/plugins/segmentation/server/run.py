"""Segment a project's z-stack in 3-D and make the result its mask.

The project's own pixels in, a label volume out (`server/volume/labels.py`):
one id per cell through every plane, which the viewer outlines plane by
plane, `measure_objects` measures whole and every cell-level tool reads like
any mask. Nothing the user brought is changed: the image is read, the label
volume is written beside the project's other derived files, and the project
is pointed at it -- a mask the project had is replaced only when asked, and
stays where it was on disk.

Resolution. Cellpose in 3-D reads every plane of every block at once, so a
large stack is segmented at a coarser XY level (`level="auto"`: the finest
whose planes hold at most `MAX_VOXELS` voxels in all) and the labels are
drawn back up to full resolution by repeating each label pixel, plane by
plane as they are written -- the mask always has the image's own size.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time

import numpy as np

from plexora.plugins.segmentation.server import cellpose3d

#: The most voxels (all planes) segmented at once, unless asked otherwise:
#: what picks the level under `level="auto"`.
MAX_VOXELS = int(os.environ.get("PLEXORA_SEGMENT3D_MAX_VOXELS", 64 * 2**20))
RECORD_NAME = "segment3d_v1.json"


class SegmentationRefused(ValueError):
    """The stack cannot be segmented as asked; the message says why."""


def _record_path(project):
    from plexora import paths

    return paths.project_state_dir(project.name) / RECORD_NAME


def last_run(project) -> dict | None:
    """What the last segmentation of this project recorded, or None."""
    path = _record_path(project)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def choose_level(volume, level) -> int:
    """`level` as asked, or under "auto" the finest XY level whose planes hold
    at most MAX_VOXELS voxels."""
    if level not in (None, "auto"):
        index = int(level)
        if not 0 <= index < len(volume.levels):
            raise SegmentationRefused(f"level {index} is not one of this image's "
                                      f"{len(volume.levels)}")
        return index
    for info in volume.levels:
        if volume.depth * info.height * info.width <= MAX_VOXELS:
            return info.index
    return volume.levels[-1].index


class _UpsampledLabels:
    """A coarse label volume read as the image's full-resolution one, a plane at
    a time (`build_label_volume` indexes `[z]`): each coarse label pixel
    repeated `factor` times along y and x, cropped to the image."""

    def __init__(self, labels, factor, height, width):
        self._labels = labels
        self._factor = factor
        self.shape = (labels.shape[0], height, width)
        self.dtype = labels.dtype

    def __getitem__(self, z):
        plane = self._labels[z]
        if self._factor > 1:
            plane = np.repeat(np.repeat(plane, self._factor, axis=0), self._factor, axis=1)
        return plane[:self.shape[1], :self.shape[2]]


def segment_project(project, *, nuclear, cytoplasm=None, mode="3d", diameter_um=None,
                    stitch_threshold=0.4, cellprob_threshold=0.0, min_size_um3=None,
                    level="auto", t=0, device="auto", replace_mask=False, measure=True,
                    progress=None, cancelled=None, stage=None) -> dict:
    """Segment `project`'s stack with Cellpose; returns the run's summary.

    `nuclear` (and optionally `cytoplasm`) name image channels. `mode` "3d"
    needs the z step (the anisotropy); "stitch" runs plane by plane. The label
    volume replaces a mask the project already has only with `replace_mask`.
    `measure` then measures every cell into the object table.
    """
    from plexora.server.utils.source_image import open_project_volume

    if mode not in ("3d", "stitch"):
        raise SegmentationRefused("mode is 3d or stitch")
    if not cellpose3d.installed():
        raise cellpose3d.CellposeMissing(
            f"Segmenting in 3-D needs Cellpose: {cellpose3d.INSTALL}")
    if project.segmentation.available and not replace_mask:
        raise SegmentationRefused(
            "this project already has a mask; ask to replace it (its file is left where "
            "it is) or segment another project")
    names = list(project.image.channel_names)
    wanted = [nuclear] + ([cytoplasm] if cytoplasm else [])
    missing = [name for name in wanted if name not in names]
    if missing:
        raise SegmentationRefused(f"no channel(s) {missing!r} in this image")
    indices = [names.index(name) for name in wanted]

    source = open_project_volume(project)
    if source is None:
        raise SegmentationRefused("this project's image is not a z-stack")
    started = time.time()
    try:
        volume = source.volume
        if not 0 <= int(t) < volume.frames:
            raise SegmentationRefused(f"frame {t} is not one of this image's {volume.frames}")
        spacing = volume.spacing
        x_um = spacing.x.microns()
        z_um = spacing.z.microns() if spacing.z.known else None
        if mode == "3d" and not z_um:
            raise SegmentationRefused(
                "segmenting in 3-D needs the distance between planes (the stack's "
                "anisotropy); set the z step, or segment plane by plane (mode stitch)")
        chosen = choose_level(volume, level)
        factor = 2 ** chosen
        info = volume.levels[chosen]
        height, width = info.height, info.width
        px_um = (x_um * factor) if x_um else None
        diameter_px = (float(diameter_um) / px_um) if (diameter_um and px_um) else None
        min_size_vox = 15
        if min_size_um3 and px_um and z_um:
            min_size_vox = max(1, int(round(float(min_size_um3) / (px_um * px_um * z_um))))
        settings = cellpose3d.Settings(
            mode=mode, anisotropy=(z_um / px_um) if (z_um and px_um) else 1.0,
            diameter_px=diameter_px, stitch_threshold=float(stitch_threshold),
            cellprob_threshold=float(cellprob_threshold), min_size_vox=min_size_vox,
            gpu=cellpose3d.use_gpu(device))
        halo = cellpose3d.halo_for(diameter_px)
        labels = np.zeros((volume.depth, height, width), dtype=np.uint32)
        plan = list(cellpose3d.blocks(height, width, cellpose3d.BLOCK, halo))
        next_id = 1
        for done, (core, padded) in enumerate(plan):
            if cancelled is not None and cancelled():
                raise InterruptedError("cancelled")
            y0, y1, x0, x1 = padded
            rows, cols = slice(y0, y1), slice(x0, x1)
            image = np.stack([np.asarray(source.read_block(chosen, int(t), index, 0,
                                                           volume.depth - 1, rows, cols))
                              for index in indices], axis=1)          # (Z, C, Y, X)
            if not image.any():
                continue
            block = cellpose3d.segment_block(image, settings)
            next_id = cellpose3d.place(labels, block, core, padded, next_id)
            if progress is not None:
                progress(done + 1, len(plan) + 1)
    finally:
        source.close()
    count = int(next_id - 1)
    if not count:
        raise SegmentationRefused("Cellpose found no cells in this stack with these settings")

    dest = _write_labels(project, labels, factor, volume.height, volume.width)
    _attach(project, dest)
    record = {
        "project": project.name, "labels": str(dest), "cells": count,
        "created": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "seconds": round(time.time() - started, 1),
        "engine": {"name": "cellpose", "version": cellpose3d.version(), "model": "Cellpose-SAM",
                   "weights": cellpose3d.WEIGHTS_PAGE, "device": "gpu" if settings.gpu else "cpu"},
        "parameters": {"nuclear": nuclear, "cytoplasm": cytoplasm, "mode": mode,
                       "diameter_um": diameter_um, "stitch_threshold": stitch_threshold,
                       "cellprob_threshold": cellprob_threshold, "min_size_um3": min_size_um3,
                       "t": int(t)},
        "resolution": {"level": chosen, "factor": factor, "anisotropy": settings.anisotropy,
                       "block": cellpose3d.BLOCK, "halo": halo},
        "licence": cellpose3d.status()["licence"],
    }
    if measure:
        from plexora.server.models.project import Project
        from plexora.server.volume import objects

        if stage is not None:
            stage("measuring")
        try:
            units = "physical" if z_um and x_um else "voxel"
            record["objects"] = objects.measure_project_objects(
                Project.load(project.name), units=units)
        except Exception as exc:  # the mask stands; measuring can be run again
            record["objects_error"] = str(exc)
    path = _record_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def _write_labels(project, labels, factor, height, width):
    """The label volume, at the image's full size, beside the project's other
    derived files."""
    from plexora import paths
    from plexora.server.volume import labels as volume_labels

    folder = paths.derived_root(project.name)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / "segment3d_labels_v1.zarr"
    source = _UpsampledLabels(labels, factor, height, width) if factor > 1 else labels
    built = volume_labels.build_label_volume(source, dest)
    import zarr

    zarr.open_group(built, mode="r+").update_attributes(
        {"source_key": f"segment3d:{dest.stat().st_mtime_ns}", "made_by": "segment3d"})
    return built


def _attach(project, dest, timeout=600.0):
    """Make the label volume the project's mask, by the path every mask takes
    (the import's), and wait until the project records it as ready -- the
    label volume is adopted as it is, so that is quick."""
    from plexora.server.models.project import Project
    from plexora.server.routes.import_routes import attach_segmentation

    attach_segmentation(project.name, str(dest))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        spec = Project.load(project.name).segmentation
        if spec.available and spec.status != "pending":
            return
        time.sleep(0.2)
    raise SegmentationRefused("the new mask was written but the project did not take it up "
                              "in time; add it from the edit page: " + str(dest))
