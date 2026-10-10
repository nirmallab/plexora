"""A projection pinned as a layer: computed once, exactly, at full resolution.

The viewer computes a slab view tile by tile, and its coarse levels are the
operator over pooled planes -- right for looking, wrong for anything that
reads numbers off it (`provenance.Provenance.exact`). Pinning computes the
projection at level 0 over every plane of the slab, writes it as a pyramidal
OME-TIFF in the project's derived folder, and registers it as an image layer.
From then on it is served, windowed, toggled, captured and exported like any
other image layer -- no route of its own -- and its file says how it was made.

    <derived root>/projections_v1/<token>.ome.tif

The layer is `proj-<token>` with modality `projection`, registered `pending`
and built by the `projection` builder (`layer_jobs.register_builder`), so its
card shows the build's progress through the poll every other layer uses.
Pinning the same view again keeps a finished layer; removing the layer
deletes the file, which -- unlike a file the user added -- Plexora made.

`sum` is not pinned: a sum of uint16 planes overflows the image's own type,
and a float32 pyramid is an export, not a layer.
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
from pathlib import Path

import numpy as np

from plexora.server.volume import reduce as volume_reduce
from plexora.server.volume.viewspec import BadView, ViewSpec

FOLDER = "projections_v1"
#: The OME Image name every pinned projection carries.
FILE_MARKER = "plexora-projection"
MODALITY = "projection"
LAYER_PREFIX = "proj-"
TILE = 1024
#: Bytes of planes held at once while the level-0 projection is computed. A
#: rank operator (median, percentile) holds every plane of a block; the others
#: hold one plane and an accumulator.
BLOCK_BUDGET = 256 * 1024 * 1024
STAGES = {
    "reading": (0, 75, "Computing the projection"),
    "building": (75, 98, "Writing the pyramid"),
}


class PinRefused(ValueError):
    """This view cannot be pinned; the message says why.

    `code` is the agent error code it maps to (plexora/agent/errors.py)."""

    def __init__(self, message, code="invalid_input"):
        super().__init__(message)
        self.code = code


_cancel: dict[tuple[str, str], threading.Event] = {}
_cancel_lock = threading.Lock()


def layer_id_for(token: str) -> str:
    return LAYER_PREFIX + token


def path_for(project_name: str, token: str) -> Path:
    from plexora import paths

    return paths.derived_root(project_name) / FOLDER / f"{token}.ome.tif"


def is_pinned_file(project_name: str, path) -> bool:
    """Whether `path` is a projection this module wrote for this project."""
    try:
        folder = (path_for(project_name, "x").parent).resolve()
        return Path(path).resolve().parent == folder
    except Exception:
        return False


def resolve(project, view) -> ViewSpec:
    """The slab `view` names on `project`'s image, or PinRefused."""
    from plexora.server.providers.base import is_remote_locator

    image = project.image
    if not image.src or not image.is_volumetric:
        raise PinRefused(f"{project.name}'s image is not a z-stack, so it has no "
                         "projections to pin")
    if is_remote_locator(image.src):
        raise PinRefused("pinning reads every plane at full resolution, which is only "
                         "done for an image on this machine", code="resource_not_local")
    try:
        spec = ViewSpec.parse(view)
    except BadView as error:
        raise PinRefused(str(error)) from None
    if spec.kind != "slab":
        raise PinRefused("only a projection (a slab such as z4-19max) can be pinned: "
                         "a plane is already the image, and a section is not in its frame")
    if spec.op == "sum":
        raise PinRefused("a sum overflows the image's pixel type; export it instead")
    depth = int(project.image.depth or 1)
    frames = int(project.image.frames or 1)
    z0, z1 = spec.planes or (0, depth - 1)
    if not (0 <= z0 <= z1 < depth) or not 0 <= spec.t < frames:
        raise PinRefused(f"{spec.token()} is outside this stack "
                         f"({depth} planes, {frames} frame(s))")
    return spec


def pin(project, view, *, replace_existing: bool = False) -> dict:
    """Register the projection as a pending layer and start its build.

    Returns `{layer, status, created}`; a layer already pinned for the same
    view is returned as it is unless `replace_existing`.
    """
    from plexora.server.models import layer_jobs
    from plexora.server.models.project import Project

    spec = resolve(project, view)
    token = spec.token()
    layer_id = layer_id_for(token)
    existing = project.layer(layer_id)
    if existing is not None and not replace_existing and existing.status in ("ready", "pending"):
        if existing.status == "ready" and Path(existing.src or "").exists():
            return {"layer": layer_id, "status": "ready", "created": False}
        running = layer_jobs.get(project.name, layer_id)
        if running and running.get("status") == "pending":
            return {"layer": layer_id, "status": "pending", "created": False}
    layer = _layer_spec(project, spec)
    saved = Project.mutate(project.name, lambda current: current.with_layer(layer))
    if saved is None:
        raise PinRefused(f"{project.name} no longer exists")
    with _cancel_lock:
        _cancel[(project.name, layer_id)] = threading.Event()
    layer_jobs.start_builder(saved, saved.layer(layer_id))
    return {"layer": layer_id, "status": "pending", "created": True}


def cancel(project_name: str, layer_id: str) -> None:
    with _cancel_lock:
        event = _cancel.get((project_name, layer_id))
    if event is not None:
        event.set()


def forget(project, layer) -> None:
    """A projection layer was removed: stop its build and delete its file."""
    cancel(project.name, layer.id)
    if layer.src and is_pinned_file(project.name, layer.src):
        try:
            Path(layer.src).unlink(missing_ok=True)
        except OSError:
            pass


def _layer_spec(project, spec: ViewSpec):
    from plexora.server.models.import_sample import DEFAULT_LAYER_COLOR, _channel_src
    from plexora.server.models.project import LayerSpec
    from plexora.server.utils.segmentation_pyramid import pyramid_factors

    token = spec.token()
    layer_id = layer_id_for(token)
    image = project.image
    label = spec.label(int(image.depth or 1), int(image.frames or 1))
    # The panel shows a layer channel's `name`; the tile route reads the index
    # off the address's `_<N>` suffix. So the name is the marker's own and the
    # address carries the index. Names must be unique within a layer.
    names, seen = [], {}
    for name in image.channel_names:
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    channels = tuple(
        {"name": name, "fullname": name,
         "src": _channel_src(project.name, layer_id, f"projection_{index}")}
        for index, name in enumerate(names))
    width, height = int(image.width), int(image.height)
    return LayerSpec(
        id=layer_id, kind="image", label=label, src=str(path_for(project.name, token)),
        modality=MODALITY, channels=channels, width=width, height=height,
        max_level=len(pyramid_factors(height, width, TILE)),
        tile_width=TILE, tile_height=TILE, transform=None,
        render={"color": DEFAULT_LAYER_COLOR}, status="pending")


# -- the build --------------------------------------------------------------


def build_layer(project, layer, stage, report) -> None:
    """`layer_jobs` builder for modality `projection`."""
    from plexora.server.utils.source_image import open_project_volume

    token = Path(layer.src).name[:-len(".ome.tif")]
    spec = resolve(project, token)
    with _cancel_lock:
        event = _cancel.setdefault((project.name, layer.id), threading.Event())
    source = open_project_volume(project)
    if source is None:
        raise PinRefused(f"{project.name}'s image could not be opened as a z-stack")
    try:
        write_projection(source, spec, Path(layer.src), project=project,
                         stage=stage, report=report, cancelled=event.is_set)
    finally:
        source.close()
        with _cancel_lock:
            _cancel.pop((project.name, layer.id), None)


def _block_side(volume, spec: ViewSpec) -> int:
    z0, z1 = spec.planes
    planes = (z1 - z0 + 1) if spec.op in ("med", "pct") else 2
    itemsize = max(np.dtype(volume.dtype).itemsize, 8 if spec.op in ("mean", "std") else 1)
    side = int((BLOCK_BUDGET / max(1, planes * itemsize)) ** 0.5)
    return max(256, min(4096, side // 256 * 256))


def write_projection(source, spec: ViewSpec, destination: Path, *, project=None,
                     stage=None, report=None, cancelled=None) -> str:
    """Compute `spec` over `source` at level 0 and write it as a pyramid."""
    from plexora.server.utils import image_pyramid, ome_zarr, segmentation_pyramid

    stage = stage or (lambda *_: None)
    report = report or (lambda *_: None)
    cancelled = cancelled or (lambda: False)
    volume = source.volume
    channels, height, width = volume.channels, volume.height, volume.width
    dtype = np.dtype(volume.dtype)
    z0, z1 = spec.planes
    factors = segmentation_pyramid.pyramid_factors(height, width, TILE)
    shapes = [(channels, -(-height // f), -(-width // f)) for f in factors]
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = image_pyramid._Scratch(shapes, dtype, destination.parent,
                                     prefix=f".{destination.name}-scratch-")
    try:
        stage("reading")
        side = _block_side(volume, spec)
        blocks = [(y, x) for y in range(0, height, side) for x in range(0, width, side)]
        total = channels * len(blocks)
        done = 0
        level0 = scratch.levels[0]
        for channel in range(channels):
            for y, x in blocks:
                if cancelled():
                    raise InterruptedError("cancelled")
                rows, cols = slice(y, min(height, y + side)), slice(x, min(width, x + side))
                level0[channel, rows, cols] = volume_reduce.reduce_planes(
                    lambda z: source.read_plane(0, spec.t, z, channel, rows, cols),
                    z0, z1, spec.op, spec.q, dtype=dtype)
                done += 1
                report(done, total)

        # Coarser levels, each reduced from the one above it the first time
        # the writer asks for it -- it asks for every tile of a level before
        # any of the next, so the level above is always complete by then.
        built = {0}

        def level(k):
            if k not in built:
                above, out = scratch.levels[k - 1], scratch.levels[k]
                band = 2 * TILE
                for channel in range(channels):
                    for y in range(0, above.shape[1], band):
                        reduced = ome_zarr._cast_like(
                            ome_zarr._reduce2(np.asarray(above[channel, y:y + band])), dtype)
                        out[channel, y // 2:y // 2 + reduced.shape[0], :reduced.shape[1]] = reduced
                built.add(k)
            return scratch.levels[k]

        def block(channel, factor, y_start, y_stop, x_start, x_stop, *_):
            if cancelled():
                raise InterruptedError("cancelled")
            k = factor.bit_length() - 1
            return np.asarray(level(k)[channel, y_start:y_stop, x_start:x_stop])

        stage("building")
        return segmentation_pyramid.write_tiled_pyramid(
            destination, height=height, width=width, dtype=dtype, block=block,
            channels=channels, metadata=_metadata(source, spec, project),
            tile_size=TILE, progress_callback=report)
    finally:
        scratch.close()


def provenance(source, spec: ViewSpec, project=None) -> dict:
    """What the file's Description says about how it was made."""
    volume = source.volume
    out = {
        "plexora": "projection",
        "view": spec.token(),
        "label": spec.label(volume.depth, volume.frames),
        "op": spec.op,
        "z0": spec.planes[0],
        "z1": spec.planes[1],
        "t": spec.t,
        "planes": spec.planes[1] - spec.planes[0] + 1,
        "exact": True,
        "level": 0,
        "dtype": str(np.dtype(volume.dtype)),
        "spacing": volume.spacing.to_dict() if hasattr(volume.spacing, "to_dict") else None,
        "created": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
    }
    if spec.op in ("med", "pct"):
        out["percentile_method"] = volume_reduce.PERCENTILE_METHOD
    if spec.op in ("mean", "std") and np.issubdtype(np.dtype(volume.dtype), np.integer):
        out["rounded"] = "to the image's integer type"
    if project is not None:
        out["project"] = project.name
        out["source"] = str(project.image.src)
    return out


def _metadata(source, spec: ViewSpec, project=None) -> dict:
    volume = source.volume
    names = list(project.image.channel_names) if project is not None else list(
        volume.channel_names or [])
    # Not image_pyramid.IMAGE_MARKER: that names a pyramidized COPY, and
    # `copy_source` would then report the stack as this file's original.
    metadata = {"axes": "CYX", "Name": FILE_MARKER,
                "Description": json.dumps(provenance(source, spec, project))}
    if names and len(names) == volume.channels:
        metadata["Channel"] = {"Name": [str(name) for name in names]}
    for axis in ("x", "y"):
        spacing = getattr(volume.spacing, axis, None)
        value = getattr(spacing, "value", None)
        if value:
            metadata[f"PhysicalSize{axis.upper()}"] = float(value)
            metadata[f"PhysicalSize{axis.upper()}Unit"] = getattr(spacing, "unit", None) or "µm"
    return metadata


def register() -> None:
    """Teach `layer_jobs` to build projection layers (idempotent)."""
    from plexora.server.models import layer_jobs

    layer_jobs.register_builder(MODALITY, build_layer, stages=STAGES)
