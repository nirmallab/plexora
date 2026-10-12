"""A project's 3-D objects as a table: measure its label volume, write it, attach it.

The bridge from a 3-D segmentation to everything Plexora does with cells.
Every cell-level tool -- gating, Cell Explorer, colour-by, the SCIMAP Pro
hand-off -- reads one row per cell from the project's table; for a 3-D
segmentation that row has to describe the whole cell, which is what
`measure.measure` computes. The result is written as a parquet file in the
project's own folder (`objects_v1.parquet`, with `objects_v1.json` saying how
it was made) and, when the project has no table yet, attached as its table
with roles `cell_id = label`, `x = X`, `y = Y`, `z = Z` -- so the label ids
ARE the cell ids and outlines, picking and colour-by line up with the rows.

A TIME SERIES is measured frame by frame: one row per object per frame, its
frame in `t` (the table's `t` role). With a label volume per frame each
frame's own objects are measured (their ids are the frame's until the frames
are linked, server/volume/tracking.py); with one label volume for every
frame, the same objects are measured at every frame -- each cell's intensity
through time.

The same pass writes the PER-PLANE CELL INDEX beside the table
(`objects_planes_v1/`, server/volume/planes.py): which cells have voxels on
each plane -- what the thresholding panel counts as the cells on the plane
on screen.

A project that already has a table keeps it: the object table is written
beside it and the caller decides (attach_as_table=True replaces it).
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import replace
from pathlib import Path

import numpy as np

from plexora.server.volume import labels as volume_labels
from plexora.server.volume import measure as volume_measure
from plexora.server.volume import planes as volume_planes

TABLE_NAME = "objects_v1.parquet"
MANIFEST_NAME = "objects_v1.json"


class ObjectsUnavailable(ValueError):
    """The project cannot be measured as 3-D objects; the message says why."""


def object_paths(project) -> tuple[Path, Path]:
    from plexora import paths

    folder = paths.project_state_dir(project.name)
    return folder / TABLE_NAME, folder / MANIFEST_NAME


def measure_project_objects(project, *, units="physical", channels=None, attach=None,
                            progress=None, cancelled=None) -> dict:
    """Measure `project`'s label volume; returns a summary dict.

    `attach`: None attaches the table only when the project has none; True
    always; False never. `cancelled()` is polled between planes.

    The pixels are read where they are. A local image is measured here; an
    image a data node holds is measured ON that node, against the mask it
    holds beside it (`providers.node.measure_objects_on_node`), because a
    whole z-stack is not something to pull across a network one strip at a
    time. Either way the table is written and attached here, by the code
    below, so a node's measurement is the same file as a local one.
    """
    seg = project.segmentation
    labels = volume_labels.project_label_volume(project)
    if labels is None:
        if seg.available:
            raise ObjectsUnavailable(
                "this project's mask is one 2-D segmentation drawn on every plane; "
                "objects in depth need a 3-D label volume (one id per cell across planes)")
        raise ObjectsUnavailable("this project has no segmentation mask")
    all_names = list(project.image.channel_names)
    wanted = list(channels) if channels else all_names
    unknown = [name for name in wanted if name not in all_names]
    if unknown:
        raise ObjectsUnavailable(f"no channel(s) {unknown!r} in this image")
    indices = [all_names.index(name) for name in wanted]

    table_path, manifest_path = object_paths(project)
    table_path.parent.mkdir(parents=True, exist_ok=True)
    planes_folder = volume_planes.folder_for(table_path)
    image_binding = project.resource("image") if hasattr(project, "resource") else None
    if image_binding is not None:
        frame, manifest = _measure_on_node(project, image_binding, wanted, indices,
                                           units, progress, cancelled, planes_folder)
    else:
        from plexora.server.utils.source_image import open_project_volume

        source = open_project_volume(project)
        if source is None:
            raise ObjectsUnavailable("this project's image is not a z-stack")
        try:
            frame, manifest = measure_columns(
                labels, source, channel_names=wanted, indices=indices, units=units,
                progress=progress, cancelled=cancelled, planes_folder=planes_folder)
        finally:
            source.close()

    tmp = table_path.with_name(table_path.name + ".tmp")
    frame.write_parquet(tmp)
    tmp.replace(table_path)
    manifest.update({
        "project": project.name,
        "labels": str(seg.derived),
        "channels": wanted,
        "created": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "table": str(table_path),
        "columns": list(frame.columns),
    })
    plane_index = volume_planes.PlaneIndex.open(planes_folder)
    if plane_index is not None:
        manifest["planes"] = {"folder": planes_folder.name, "rows": plane_index.rows}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    attached = False
    if attach is True or (attach is None and not project.has_table):
        attach_object_table(project, table_path, frame)
        attached = True
    return {"objects": int(frame.height), "table": str(table_path), "attached": attached,
            "units": manifest["units"], "columns": list(frame.columns),
            "spacing_um": manifest.get("spacing_um"), "manifest": str(manifest_path)}


def measure_columns(labels, source, *, channel_names, indices, units="physical",
                    progress=None, cancelled=None, planes_folder=None):
    """`(frame, manifest)`: every object of `labels` measured against the
    image `source` (an open volume source), one row per object per frame.

    The half of `measure_project_objects` that reads pixels, and so the half
    that runs where they are: here for a local image, on the data node for one
    held there (node/api.volume_measure). `labels` is a label volume -- a
    local store, or the node's own. `indices` are the image's channels to read,
    named `channel_names` in the table. With `planes_folder` the per-plane
    cell index is written there too (planes.py), from the same reads.
    """
    import polars as pl

    volume = source.volume
    if (labels.depth, labels.height, labels.width) != (volume.depth, volume.height,
                                                        volume.width):
        raise ObjectsUnavailable(
            f"the mask is {labels.depth}x{labels.height}x{labels.width} (z, y, x) and "
            f"the image {volume.depth}x{volume.height}x{volume.width}; they do not "
            "describe the same volume")
    frames = max(1, int(volume.frames))
    if labels.frames > 1 and labels.frames != frames:
        raise ObjectsUnavailable(
            f"the mask has {labels.frames} frames and the image {frames}; "
            "they do not describe the same series")

    writer = None
    if planes_folder is not None:
        writer = volume_planes.PlaneWriter(planes_folder, depth=volume.depth, frames=frames)
    try:
        parts, manifest = _measure_frames(labels, source, channel_names, indices, units,
                                          frames, progress, cancelled, writer)
    except BaseException:
        if writer is not None:
            writer.abort()
        raise
    if writer is not None:
        writer.close(labels_per_frame=labels.frames > 1)
    frame = pl.concat([pl.DataFrame({name: np.asarray(values) for name, values in part.items()})
                       for part in parts], how="vertical")
    if frames > 1:
        manifest["frames"] = frames
        manifest["labels_per_frame"] = labels.frames > 1
    return frame, manifest


def _measure_frames(labels, source, channel_names, indices, units, frames, progress,
                    cancelled, writer):
    volume = source.volume
    parts, manifest = [], None
    for t in range(frames):
        def read(index, z, rows, cols, t=t):
            if cancelled is not None and cancelled():
                raise InterruptedError("cancelled")
            level = source.level_array(t, z, 0)
            return np.asarray(level[indices[index], rows, cols])

        def step(done, total, t=t):
            if progress is not None:
                progress(t * total + done, frames * total)

        columns, manifest = volume_measure.measure(
            labels.frame(t), read, channel_names=list(channel_names), spacing=volume.spacing,
            units=units, t=t, progress=step, planes=writer)
        parts.append(columns)
    return parts, manifest


def _measure_on_node(project, image_binding, wanted, indices, units, progress, cancelled,
                     planes_folder=None):
    """`measure_columns`, run on the data node that holds the image -- which
    has to hold the mask too: measuring reads both, plane by plane, and the
    one machine that has both files is the one to read them on."""
    from plexora.server.providers import node as node_provider
    from plexora.server.providers.base import ResourceError

    seg_binding = project.resource("segmentation")
    if seg_binding is None or seg_binding.node != image_binding.node:
        raise ObjectsUnavailable(
            f"this project's image is on data node {image_binding.node!r} and its mask "
            "is not; 3-D objects are measured on the machine that holds both, so share "
            "the mask on that node too")
    spec = project.image
    try:
        return node_provider.measure_objects_on_node(
            image_binding, seg_binding, channel_names=wanted, indices=indices, units=units,
            z_semantics=spec.z_semantics, t_semantics=spec.t_semantics,
            pixel_size=spec.pixel_size, pixel_size_z=spec.pixel_size_z,
            frame_interval=spec.frame_interval, progress=progress, cancelled=cancelled,
            planes_folder=planes_folder)
    except ResourceError as exc:
        raise ObjectsUnavailable(str(exc)) from exc


def attach_object_table(project, table_path, frame=None):
    """Make the object table the project's cell table (roles label/X/Y/Z)."""
    from plexora.datasource import flat_table_spec
    from plexora.server.models.project import Project

    import polars as pl

    if frame is None:
        frame = pl.read_parquet(table_path, n_rows=1)
    schema = [{"name": name, "dtype": str(dtype)} for name, dtype in frame.schema.items()]
    spec = flat_table_spec(str(table_path), schema, x="X", y="Y", id_column="label",
                           data_type="parquet")
    # A time series' rows name their frame; one frame's table needs no `t`.
    series = "t" in frame.columns and pl.read_parquet(
        table_path, columns=["t"])["t"].n_unique() > 1
    # Measured from this project's own image, so it covers exactly one image:
    # an answer, not a missing image-id column (gating asks for one).
    spec = replace(spec, roles=replace(spec.roles, z="Z", t="t" if series else None),
                   single_image=True)
    fresh = Project.load(project.name)
    # The roles are this module's own columns, so they are facts rather than
    # guesses to put in front of the user; which columns are markers is still
    # theirs to check once. Answers about a table this replaces go first.
    confirmed = ("table", "role:cell_id", "role:x", "role:y", "role:z", "role:image_id")
    fresh.forget_table_answers().patch(dataset=spec).with_confirmed(
        confirmed + (("role:t",) if series else ())).save()


def note_table_column(project, table_path, column):
    """A column added to the object table after it was attached (`track`):
    recorded with the table's other non-marker columns, so the project's
    reading of the table keeps every answer it had."""
    from plexora.server.models.project import Project

    fresh = Project.load(project.name)
    spec = fresh.dataset
    if spec is None or str(spec.src) != str(table_path):
        return False
    if column in spec.columns.metadata or column in spec.columns.markers:
        return True
    columns = replace(spec.columns, metadata=tuple(spec.columns.metadata) + (column,))
    fresh.patch(dataset=replace(spec, columns=columns)).save()
    return True
