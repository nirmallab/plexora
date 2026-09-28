"""What kind of project was opened, and how long opening it took -- as kinds
and bands, never as content.

`on_load_finished` is called by `data_model.LoadProgress.finish`, under the
load lock, so it only captures a few O(1) facts there and hands the rest to
the telemetry writer thread (`telemetry.defer`). The descriptor reads the
project's own record -- kinds, sizes, counts -- and never a pixel, a column
name, a marker, a path or the project's name. The name is used for one thing,
deduplicating "opened this project already this session", and only as an
in-memory hash (`Telemetry.note_project`).
"""

from __future__ import annotations

from time import perf_counter

from plexora.telemetry import schema
from plexora.telemetry.client import telemetry


def _enum(value, allowed, fallback="other"):
    return value if value in allowed else fallback


def _tile(size):
    return str(size) if size in (256, 512, 1024) else "other"


def _bit_depth(dtype_name):
    name = str(dtype_name or "")
    if name in ("uint8", "int8"):
        return "8"
    if name in ("uint16", "int16"):
        return "16"
    if name in ("uint32", "int32"):
        return "32"
    if name.startswith("float"):
        return "float"
    return "unknown"


def describe(project, facts=None) -> dict:
    """The `dataset.opened` props for `project`. Every field is attempted on
    its own and left out if it cannot be read: a descriptor with a gap is
    better than none, and none is better than an exception."""
    facts = facts or {}
    props = {}

    def put(key, compute):
        try:
            value = compute()
        except Exception:
            return
        if value is not None:
            props[key] = value

    try:
        image = project.image
    except Exception:
        image = None
    put("image_kind", lambda: _enum(getattr(image, "kind", None) or "none",
                                    schema.IMAGE_KINDS))
    put("modality", lambda: _enum(getattr(image, "modality", None) or "none", schema.MODALITIES))
    put("image_type", lambda: project.image_type
        if project.image_type in ("brightfield", "fluorescence") else None)
    put("width", lambda: schema.band10(image.width) if image.width else None)
    put("height", lambda: schema.band10(image.height) if image.height else None)
    put("pixels", lambda: schema.band10(int(image.width) * int(image.height))
        if image.width and image.height else None)
    put("levels", lambda: min(32, int(image.max_level) + 1) if image.max_level is not None else None)
    put("tile", lambda: _tile(image.tile_width) if image.tile_width else None)
    channels = None
    try:
        channels = len(image.real_channels)
    except Exception:
        pass
    if channels is not None:
        props["channels_band"] = schema.band_pow2(channels)
        props["channels"] = min(4096, channels)
    if getattr(image, "kind", None) in ("brightfield", "rgb"):
        props["bit_depth"] = "rgb"
    elif facts.get("dtype"):
        props["bit_depth"] = _bit_depth(facts["dtype"])
    try:
        segmentation = project.segmentation
    except Exception:
        segmentation = None

    def seg_kind():
        if segmentation is None or not (segmentation.derived or segmentation.source):
            return "none"
        return "derived" if segmentation.derived and not segmentation.source else "mask"

    put("segmentation", seg_kind)
    put("segmentation_status", lambda: _enum(segmentation.status,
                                             ("ready", "pending", "building", "failed"))
        if segmentation is not None and props.get("segmentation") != "none" else None)
    put("table_kind", lambda: _enum(project.source_kind or "none", schema.TABLE_KINDS))
    if facts.get("rows") is not None:
        props["rows"] = schema.band10(facts["rows"])
    put("markers", lambda: schema.band_pow2(len(project.columns.markers)))
    put("metadata", lambda: schema.band_pow2(len(project.columns.metadata)))

    def layers():
        counts = {"image": 0, "labels": 0, "points": 0, "shapes": 0}
        for layer in project.all_layers:
            if layer.kind in counts:
                counts[layer.kind] = min(256, counts[layer.kind] + 1)
        return counts

    put("layers", layers)
    put("distributed", lambda: bool(project.is_distributed))
    put("node_backed", lambda: bool(facts.get("node_backed")))

    def bundles():
        formats = []
        for bundle in project.bundles or ():
            fmt = _enum(bundle.get("format"), schema.BUNDLE_FORMATS)
            if fmt not in formats:
                formats.append(fmt)
        return formats[:8] or None

    put("bundle_formats", bundles)
    return props


class LoadTimer:
    """Stage timestamps for one load, kept by `LoadProgress`."""

    __slots__ = ("start", "stages")

    def __init__(self):
        self.start = perf_counter()
        self.stages = []

    def stage(self, key):
        self.stages.append((key, perf_counter()))

    def durations(self, end=None) -> dict:
        end = perf_counter() if end is None else end
        found = {}
        marks = self.stages + [("end", end)]
        for (key, at), (_next, until) in zip(marks, marks[1:]):
            found[key] = found.get(key, 0.0) + (until - at) * 1000.0
        found["total"] = (end - self.start) * 1000.0
        return found


def outcome_of(error) -> str:
    if error is None:
        return "ready"
    if not isinstance(error, Exception):
        return "error"
    try:
        from plexora.server.models.data_model import classify_image_error

        status = classify_image_error(error)[0]
    except Exception:
        return "error"
    return status if status in schema.LOAD_OUTCOMES else "error"


def on_load_finished(name, project, timer, error=None, facts=None):
    """Record one load. Called from `LoadProgress.finish`; never raises."""
    if not telemetry.enabled or project is None:
        return
    try:
        durations = timer.durations() if timer is not None else {}
        facts = dict(facts or {})
        modality = _enum(getattr(project.image, "modality", None) or "none", schema.MODALITIES)
        record = {
            "outcome": outcome_of(error),
            "remote": bool(facts.get("remote")),
            "node_backed": bool(facts.get("node_backed")),
            "modality": modality,
        }
        for stage in ("table", "segmentation", "image", "total"):
            if stage in durations:
                record[f"{stage}_ms"] = schema.band_ms(durations[stage])
        telemetry.emit("project.load", record)
        if error is None and telemetry.note_project(name):
            telemetry.defer(lambda: telemetry.emit("dataset.opened", describe(project, facts)))
    except Exception:
        telemetry._fail()
