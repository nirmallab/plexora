"""The surface layer: what a surface is on disk, and how it is built and removed.

Modelled on a pinned projection (server/volume/pin.py): a surface is a layer
(`kind="surface"`, `modality="surface"`, id `surf-<token>`) registered
`pending` and built in the background by the `surface` builder
(`layer_jobs.register_builder`), so its card follows the build through the
poll every layer uses. Its files live in the project's derived folder:

    <derived root>/surfaces_v1/<token>/manifest.json     how it was made
    <derived root>/surfaces_v1/<token>/mesh-L<k>.bin     the mesh, level k
    <derived root>/surfaces_v1/<token>/objects.parquet   one row per object

The token is a hash of everything the surface depends on -- the image or
mask file (path, size, time), the channel, frame, isovalue, preprocessing,
size filter, smoothing, quality and region -- so asking for the same surface
twice returns the one that exists, and a changed source is a new surface.
The request itself rides in the layer's entry (`surface`), so a failed build
is retried from the card with exactly what was asked.

Removing the layer deletes the folder: Plexora made it.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from plexora.server.surface import isosurface, levels, marching
from plexora.server.surface import labels as label_surfaces
from plexora.server.surface import mesh as mesh_mod

FOLDER = "surfaces_v1"
MODALITY = "surface"
KIND = "surface"
LAYER_PREFIX = "surf-"
MANIFEST = "manifest.json"
OBJECTS = "objects.parquet"
MANIFEST_VERSION = 1
TYPES = ("isosurface", "label_surfaces", "imported")
ALGORITHM = "marching_cubes_lewiner"
#: How cell surfaces are made (surface/nets.py). Part of their cache key, so a
#: change of engine marks the cell surfaces made before it stale -- and only them.
LABEL_ALGORITHM = "surface_nets_multilabel"
LABEL_ENGINE = 2
#: What a surface's `role` may be: "mask" is the viewer's own cell surfaces,
#: built when Outlines or Filled is chosen in 3-D (services/maskSurfaces.js).
ROLES = ("mask",)
STAGES = {
    "scanning": (0, 10, "Finding the objects"),
    "extracting": (10, 70, "Extracting the surface"),
    "smoothing": (70, 85, "Smoothing the surface"),
    "measuring": (85, 95, "Measuring the objects"),
    "writing": (95, 99, "Writing the surface"),
}

_cancel: dict[tuple[str, str], threading.Event] = {}
_cancel_lock = threading.Lock()


class SurfaceRefused(levels.SurfaceRefused):
    pass


# -- the request --------------------------------------------------------------------


def _number(raw, key, default=None, *, low=None):
    value = raw.get(key, default)
    if value is None:
        return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise SurfaceRefused(f"{key} is a number") from None
    if not math.isfinite(value) or (low is not None and value < low):
        raise SurfaceRefused(f"{key} is a number of at least {low}")
    return value


@dataclass(frozen=True)
class SurfaceSpec:
    """What a surface is asked to be. `to_dict` is what is stored and hashed."""

    type: str
    channel: str | None = None
    t: int = 0
    iso: float | None = None
    preprocess: isosurface.Preprocess = field(default_factory=isosurface.Preprocess)
    min_size: float = 0.0
    smoothing: str = "light"
    quality: str = "standard"
    level: int | None = None
    region: dict | None = None
    ids: tuple | None = None
    inset: float = label_surfaces.DEFAULT_INSET
    label: str | None = None
    role: str | None = None

    @classmethod
    def from_dict(cls, raw) -> "SurfaceSpec":
        raw = raw if isinstance(raw, dict) else {}
        kind = raw.get("type") or "isosurface"
        if kind not in ("isosurface", "label_surfaces"):
            raise SurfaceRefused("type is isosurface or label_surfaces")
        smoothing = str(raw.get("smoothing") or ("light" if kind == "isosurface" else "medium"))
        if smoothing not in mesh_mod.Smoothing.PRESETS:
            raise SurfaceRefused(f"smoothing is one of {', '.join(mesh_mod.Smoothing.PRESETS)}")
        quality = str(raw.get("quality") or "standard")
        if quality not in levels.QUALITY:
            raise SurfaceRefused(f"quality is one of {', '.join(levels.QUALITIES)}")
        level = raw.get("level")
        if level not in (None, "", "auto"):
            try:
                level = int(level)
            except (TypeError, ValueError):
                raise SurfaceRefused("level is a whole number or auto") from None
        else:
            level = None
        try:
            t = max(0, int(raw.get("t") or 0))
        except (TypeError, ValueError):
            raise SurfaceRefused("t is a whole number") from None
        ids = raw.get("ids")
        if ids is not None:
            try:
                ids = tuple(sorted({int(v) for v in ids if int(v) > 0}))
            except (TypeError, ValueError):
                raise SurfaceRefused("ids are object ids (whole numbers)") from None
            if not ids:
                raise SurfaceRefused("ids names no object")
        iso = _number(raw, "iso") if kind == "isosurface" else None
        if kind == "isosurface" and iso is None:
            raise SurfaceRefused("an isosurface needs iso, the value in the channel's raw units")
        channel = raw.get("channel")
        if kind == "isosurface" and not channel:
            raise SurfaceRefused("an isosurface needs the channel it is of")
        region = raw.get("region") if isinstance(raw.get("region"), dict) else None
        label = str(raw.get("label") or "").strip()[:120] or None
        role = raw.get("role") or None
        if role is not None and (kind != "label_surfaces" or role not in ROLES):
            raise SurfaceRefused("role is mask, and only for cell surfaces")
        return cls(type=kind, channel=str(channel) if channel else None, t=t, iso=iso,
                   preprocess=isosurface.Preprocess.from_dict(raw.get("preprocess")),
                   min_size=_number(raw, "min_size", 0.0, low=0.0), smoothing=smoothing,
                   quality=quality, level=level, region=region, ids=ids,
                   inset=_number(raw, "inset", label_surfaces.DEFAULT_INSET, low=0.0),
                   label=label, role=role)

    def to_dict(self) -> dict:
        out = {"type": self.type, "t": self.t, "smoothing": self.smoothing,
               "quality": self.quality, "min_size": self.min_size}
        if self.channel:
            out["channel"] = self.channel
        if self.iso is not None:
            out["iso"] = self.iso
        if self.preprocess.active:
            out["preprocess"] = self.preprocess.to_dict()
        if self.level is not None:
            out["level"] = self.level
        if self.region:
            out["region"] = self.region
        if self.ids is not None:
            out["ids"] = list(self.ids)
        if self.type == "label_surfaces":
            out["inset"] = self.inset
        if self.label:
            out["label"] = self.label
        if self.role:
            out["role"] = self.role
        return out

    def default_label(self, n_ids=None) -> str:
        if self.label:
            return self.label
        if self.type == "isosurface":
            return f"{self.channel} surface at {self.iso:g}"
        if self.ids is not None:
            return f"Surfaces of {len(self.ids)} cell{'s' if len(self.ids) != 1 else ''}"
        return "Cell surfaces"


# -- names --------------------------------------------------------------------------


def surfaces_root(project_name: str) -> Path:
    from plexora import paths

    return paths.derived_root(project_name) / FOLDER


def folder_for(project_name: str, token: str) -> Path:
    return surfaces_root(project_name) / token


def layer_id_for(token: str) -> str:
    return LAYER_PREFIX + token


def token_of(layer_id: str) -> str:
    return str(layer_id)[len(LAYER_PREFIX):] if str(layer_id).startswith(LAYER_PREFIX) else ""


def is_surface_folder(project_name: str, path) -> bool:
    try:
        return Path(path).resolve().parent == surfaces_root(project_name).resolve()
    except Exception:
        return False


def _node_binding(project, kind):
    """The data-node binding of the project's `kind` ("image"/"segmentation"),
    or None when that file is on this machine."""
    binding = (getattr(project, "resources", None) or {}).get(kind)
    return binding if binding is not None and getattr(binding, "node", None) else None


def _identity(project, kind) -> str:
    """What the image or mask is, for the cache key: path, size and time for a
    file here; for one a data node holds, its locator and the fingerprint the
    node recorded when it was attached (the file is on the other machine, so
    a fresh stat would be a round trip per key)."""
    from plexora.server.volume.serve import source_identity

    binding = _node_binding(project, kind)
    if binding is not None:
        return (f"node://{binding.node}/{binding.resource_id}|"
                f"{json.dumps(binding.fingerprint or {}, sort_keys=True, default=str)}")
    path = project.image.src if kind == "image" else project.segmentation.derived
    return source_identity(path)


def _provenance(project, kind) -> str:
    """Where the image or mask was read from, as the manifest records it."""
    binding = _node_binding(project, kind)
    if binding is not None:
        return f"node://{binding.node}/{binding.resource_id}"
    return str(project.image.src if kind == "image" else project.segmentation.derived)


def cache_key(project, spec: SurfaceSpec) -> str:
    """sha256 over the source's identity and every parameter that shapes the
    mesh (the label shown on the card is not one)."""
    params = spec.to_dict()
    params.pop("label", None)
    parts = {"image": _identity(project, "image"), "params": params,
             "manifest": MANIFEST_VERSION}
    if spec.type == "label_surfaces":
        parts["labels"] = _identity(project, "segmentation")
        parts["engine"] = [LABEL_ALGORITHM, LABEL_ENGINE]
    raw = json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# -- checks before anything is registered -------------------------------------------


def check(project, spec: SurfaceSpec, *, source=None) -> dict:
    """Refuse what cannot be built, before a layer exists. Returns what is
    known about the build (`plan` for an isosurface, `objects` for labels)."""
    from plexora.server.providers.base import is_remote_locator

    image = project.image
    # A node-held image has no src here; its binding says where it is.
    has_image = bool(image.src) or _node_binding(project, "image") is not None
    if not has_image or not image.is_volumetric:
        raise SurfaceRefused(f"{project.name}'s image is not a z-stack, so it has no "
                             "surfaces to extract")
    # A web address is read by this process, a block at a time over the
    # internet -- not a build to run. A data node is different: it builds the
    # surface next to its files (`build_layer`), so it is not refused here.
    if is_remote_locator(image.src):
        raise SurfaceRefused("surfaces are extracted from an image on this machine or on a "
                             "data node; this one is read from a web address",
                             code="resource_not_local")
    frames = int(image.frames or 1)
    if not 0 <= spec.t < frames:
        raise SurfaceRefused(f"t {spec.t} is outside this image's {frames} frame(s)")
    if spec.type == "isosurface":
        names = list(image.channel_names)
        if spec.channel not in names:
            raise SurfaceRefused(f"no channel {spec.channel!r} in this image")
        if source is None:
            return {}
        volume = source.volume
        region = levels.Region.from_dict(spec.region, width=volume.width, height=volume.height,
                                         depth=volume.depth)
        plan = levels.plan_isosurface(volume, region, quality=spec.quality, level=spec.level)
        return {"plan": plan.to_dict()}
    from plexora.server.volume import labels as volume_labels

    # Wherever the mask is held: a data node's label volume answers the
    # planning below from its description (providers/node.NodeLabelVolume).
    if not volume_labels.project_has_label_volume(project):
        raise SurfaceRefused("surfaces of cells need a 3-D mask (a label volume, one id per "
                             "cell across planes); this project has none")
    table = object_table(project, spec.t)
    if table is None:
        return {}
    keep = table if spec.ids is None else table.filter(
        __import__("polars").col("label").is_in(list(spec.ids)))
    if spec.ids is not None and keep.height == 0:
        raise SurfaceRefused("none of those ids is an object of this mask")
    faces = _faces(keep)
    labels_volume = volume_labels.project_label_volume(project, spec.t)
    if labels_volume is None:
        raise SurfaceRefused("this project's 3-D mask could not be opened")
    scale, physical = project_scale(project)
    level, estimate = levels.plan_labels(labels_volume, keep["volume_vox"].to_numpy(),
                                         faces=faces, quality=spec.quality, level=spec.level,
                                         image_width=image.width,
                                         scale=scale if physical else None)
    return {"objects": int(keep.height), "level": level, "triangles": estimate}


def object_table(project, t=0):
    """The measured object table (objects_v1.parquet), frame `t`'s rows, or None."""
    from plexora.server.volume import objects as volume_objects

    path, _ = volume_objects.object_paths(project)
    if not path.exists():
        return None
    try:
        import polars as pl

        frame = pl.read_parquet(path)
    except Exception:
        return None
    needed = {"label", "bbox_x0", "bbox_x1", "bbox_y0", "bbox_y1", "bbox_z0", "bbox_z1",
              "volume_vox"}
    if not needed <= set(frame.columns):
        return None
    if "t" in frame.columns and frame["t"].n_unique() > 1:
        frame = frame.filter(frame["t"] == int(t))
    return frame


def _faces(frame):
    if {"faces_x", "faces_y", "faces_z"} <= set(frame.columns):
        return (frame["faces_x"].to_numpy(), frame["faces_y"].to_numpy(),
                frame["faces_z"].to_numpy())
    return None


# -- registering --------------------------------------------------------------------


def create(project, raw, *, replace_existing=False, source=None, render=None) -> dict:
    """Register the surface as a pending layer and start its build.

    `{layer, status, created, ...check}`; the same request again returns the
    layer that exists unless `replace_existing`."""
    from plexora.server.models import layer_jobs
    from plexora.server.models.project import LayerSpec, Project

    spec = SurfaceSpec.from_dict(raw)
    known = check(project, spec, source=source)
    key = cache_key(project, spec)
    token = key[:12]
    layer_id = layer_id_for(token)
    existing = project.layer(layer_id)
    if existing is not None and not replace_existing and existing.status in ("ready", "pending"):
        if existing.status == "ready" and (Path(existing.src or "") / MANIFEST).exists():
            return {"layer": layer_id, "status": "ready", "created": False, **known}
        running = layer_jobs.get(project.name, layer_id)
        if running and running.get("status") == "pending":
            return {"layer": layer_id, "status": "pending", "created": False, **known}
    folder = folder_for(project.name, token)
    bag = {"color": "#d8b45a" if spec.type == "isosurface" else "#9fb7c9",
           "style": "solid", "material": "soft", "opacity": 1.0}
    if spec.type == "label_surfaces":
        bag["colorBy"] = "id"
    if isinstance(render, dict):
        bag.update({k: v for k, v in render.items() if isinstance(k, str)})
    image = project.image
    layer = LayerSpec(
        id=layer_id, kind=KIND, label=spec.default_label(), src=str(folder),
        modality=MODALITY, width=int(image.width or 0) or None,
        height=int(image.height or 0) or None, render=bag, status="pending",
        extra={"surface": spec.to_dict(), "cacheKey": key})
    saved = Project.mutate(project.name, lambda current: current.with_layer(layer))
    if saved is None:
        raise SurfaceRefused(f"{project.name} no longer exists")
    with _cancel_lock:
        _cancel[(project.name, layer_id)] = threading.Event()
    register()
    layer_jobs.start_builder(saved, saved.layer(layer_id))
    return {"layer": layer_id, "status": "pending", "created": True, **known}


def cancel(project_name: str, layer_id: str) -> None:
    with _cancel_lock:
        event = _cancel.get((project_name, layer_id))
    if event is not None:
        event.set()


def forget(project, layer) -> None:
    """A surface layer was removed: stop its build and delete its folder."""
    cancel(project.name, layer.id)
    if layer.src and is_surface_folder(project.name, layer.src):
        shutil.rmtree(layer.src, ignore_errors=True)


def surface_layers(project) -> list:
    return [layer for layer in project.spatial_layers if layer.kind == KIND]


def read_manifest(layer) -> dict | None:
    try:
        return json.loads((Path(layer.src) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


# -- the build ----------------------------------------------------------------------


def _scale(volume):
    spacing = volume.spacing
    if spacing.volumetric_ok():
        return (spacing.x.microns(), spacing.y.microns(), spacing.z.microns()), True
    return (1.0, 1.0, 1.0), False


def open_volume_for(project):
    """The project's z-stack for a surface: opened here when the file is on
    this machine; for one a data node holds, its description plus block reads
    (`providers.node.project_volume_reader`) -- enough to plan, preview and
    measure, while a build runs on the node. None when it is neither. The
    caller closes what comes back."""
    from plexora.server.utils.source_image import open_project_volume

    if _node_binding(project, "image") is not None:
        from plexora.server.providers import node as node_provider

        return node_provider.project_volume_reader(project)
    return open_project_volume(project)


def _builds_on(project, spec):
    """The data-node binding a build of `spec` runs on, or None for here.

    Where the voxels it reads are: an isosurface reads the image's channel,
    cell surfaces read only the mask (the image gives the spacing, which is
    sent along). Both are minutes to hours of reading, and only the mesh
    should cross the network."""
    return _node_binding(project, "image" if spec.type == "isosurface" else "segmentation")


def build_layer(project, layer, stage, report) -> None:
    """`layer_jobs` builder for modality `surface`."""
    spec = SurfaceSpec.from_dict((layer.extra or {}).get("surface"))
    with _cancel_lock:
        event = _cancel.setdefault((project.name, layer.id), threading.Event())
    folder = Path(layer.src)
    source = open_volume_for(project)
    if source is None:
        raise SurfaceRefused(f"{project.name}'s image could not be opened as a z-stack")
    try:
        if _builds_on(project, spec) is not None:
            surface, level, info = _build_on_node(project, spec, source, stage, report,
                                                  event.is_set)
        elif spec.type == "isosurface":
            surface, level, info = _build_isosurface(project, spec, source, stage, report,
                                                     event.is_set)
        else:
            surface, level, info = _build_label_surfaces(project, spec, source, stage, report,
                                                         event.is_set)
        if surface.n_triangles > levels.RESIDENT_TRIANGLES:
            raise SurfaceRefused(
                f"the surface has {surface.n_triangles / 1e6:.1f} M triangles, more than a "
                f"browser can hold ({levels.RESIDENT_TRIANGLES / 1e6:.0f} M); choose a lower "
                "quality, a smaller region or fewer objects")
        stage("measuring")
        volume = source.volume
        scale, physical = _scale(volume)
        columns = mesh_mod.measure(surface, scale=scale, physical=physical,
                                   dims=(volume.width, volume.height, volume.depth))
        frame = _objects_frame(project, spec, columns)
        stage("writing")
        write_surface(folder, surface, frame, project=project, spec=spec, level=level,
                      volume=volume, scale=scale, physical=physical, info=info,
                      cache_key=(layer.extra or {}).get("cacheKey"))
    finally:
        source.close()
        with _cancel_lock:
            _cancel.pop((project.name, layer.id), None)


def _channel_index(project, spec):
    names = list(project.image.channel_names)
    if spec.channel not in names:
        raise SurfaceRefused(f"no channel {spec.channel!r} in this image")
    return names.index(spec.channel)


def _build_isosurface(project, spec, source, stage, report, cancelled):
    return extract_isosurface(spec, source, _channel_index(project, spec), stage, report,
                              cancelled)


def _build_label_surfaces(project, spec, source, stage, report, cancelled):
    from plexora.server.volume import labels as volume_labels

    volume = source.volume
    if not volume_labels.project_has_label_volume(project):
        raise SurfaceRefused("this project has no 3-D mask")
    labels_volume = volume_labels.project_label_volume(project, spec.t)
    if labels_volume is None:
        raise SurfaceRefused("this project's 3-D mask could not be opened")
    scale, physical = _scale(volume)
    return extract_label_surfaces(spec, labels_volume, object_table(project, spec.t),
                                  image_width=volume.width, scale=scale, physical=physical,
                                  stage=stage, report=report, cancelled=cancelled)


#: The object-table columns a cell-surface build reads (boxes and sizes).
TABLE_COLUMNS = ("label", "bbox_x0", "bbox_x1", "bbox_y0", "bbox_y1", "bbox_z0", "bbox_z1",
                 "volume_vox", "faces_x", "faces_y", "faces_z")


def _build_on_node(project, spec, source, stage, report, cancelled):
    """The extraction, run by the data node that holds the voxels it reads
    (`extract_on_node`, over there): only the mesh comes back. Measuring and
    writing the surface happen here, from the same mesh, exactly as for a
    file on this machine."""
    import io

    from plexora.server.providers import node as node_provider
    from plexora.server.providers.base import ResourceError

    volume = source.volume
    image_spec = project.image
    body = {"spec": spec.to_dict(), "zs": image_spec.z_semantics,
            "ts": image_spec.t_semantics, "pixel_size": image_spec.pixel_size,
            "pixel_size_z": image_spec.pixel_size_z,
            "frame_interval": image_spec.frame_interval}
    if spec.type == "isosurface":
        binding = _node_binding(project, "image")
        body.update(image=binding.resource_id, channel=_channel_index(project, spec))
    else:
        binding = _node_binding(project, "segmentation")
        scale, physical = _scale(volume)
        body.update(segmentation=binding.resource_id, image_width=int(volume.width),
                    scale=list(scale), physical=bool(physical))
        table = object_table(project, spec.t)
        if table is not None:
            import base64

            buffer = io.BytesIO()
            table.select([c for c in TABLE_COLUMNS if c in table.columns]).write_parquet(buffer)
            body["table"] = base64.b64encode(buffer.getvalue()).decode("ascii")
    try:
        return node_provider.build_surface_on_node(binding, body, stage=stage, progress=report,
                                                   cancelled=cancelled)
    except ResourceError as exc:
        raise SurfaceRefused(str(exc)) from exc


def extract_on_node(body, *, image=None, labels=None, stage, report, cancelled):
    """A data node's half of `_build_on_node`: `(surface, level, info)` from
    the files this node holds. `image` is a callable opening the image as a
    volume under the primary's z/t reading (isosurfaces), `labels` the mask's
    label volume store path (cell surfaces)."""
    import base64
    import io

    from dataclasses import replace

    spec = SurfaceSpec.from_dict(body.get("spec"))
    if spec.type == "isosurface":
        source = image(body.get("zs") or None, body.get("ts") or None)
        if source is None or source.volume.is_planar:
            raise SurfaceRefused("this node reads the image as a single plane, so it has no "
                                 "surfaces to extract")
        try:
            spacing = source.volume.spacing.overlaid(
                pixel_size=body.get("pixel_size"), pixel_size_z=body.get("pixel_size_z"),
                frame_interval=body.get("frame_interval"), frames=source.volume.frames)
            if spacing is not source.volume.spacing:
                source.volume = replace(source.volume, spacing=spacing)
            return extract_isosurface(spec, source, int(body.get("channel") or 0), stage,
                                      report, cancelled)
        finally:
            source.close()
    from plexora.server.volume import labels as volume_labels

    if not labels or not volume_labels.is_label_volume(labels):
        raise SurfaceRefused("this node holds the mask as one 2-D segmentation, not a label "
                             "volume")
    table = None
    if body.get("table"):
        import polars as pl

        table = pl.read_parquet(io.BytesIO(base64.b64decode(body["table"])))
    scale = tuple(float(v) for v in body.get("scale") or (1.0, 1.0, 1.0))
    return extract_label_surfaces(spec, volume_labels.open_label_volume(labels, spec.t), table,
                                  image_width=body.get("image_width"), scale=scale,
                                  physical=bool(body.get("physical", True)),
                                  stage=stage, report=report, cancelled=cancelled)


def extract_isosurface(spec, source, channel, stage, report, cancelled):
    """`(surface, level, info)`: the isosurface of channel index `channel` of
    `source` (anything with `.volume` and `read_block`). Run here for a file
    on this machine and by a data node for one it holds."""
    volume = source.volume
    region = levels.Region.from_dict(spec.region, width=volume.width, height=volume.height,
                                     depth=volume.depth)
    plan = levels.plan_isosurface(volume, region, quality=spec.quality, level=spec.level)
    scale, _ = _scale(volume)
    stage("extracting")
    surface, rep = isosurface.extract(
        levels.plan_reader(source, plan, t=spec.t, channel=channel), plan.grid, spec.iso,
        pre=spec.preprocess, voxel_um=plan.voxel_um(scale), scale=scale,
        min_size=spec.min_size, smoothing=mesh_mod.Smoothing.preset(spec.smoothing),
        progress=report, cancelled=cancelled)
    return surface, plan.level, {"plan": plan.to_dict(), "report": rep,
                                 "region": region.to_dict(), "ids_from": "components"}


def extract_label_surfaces(spec, labels_volume, table, *, image_width, scale, stage, report,
                           cancelled, physical=True, threads=None):
    """`(surface, level, info)`: one surface per object of `labels_volume`
    (frame `spec.t`'s), sized by the object table `table` when there is one
    and by a count on the mask's coarsest level when not. `scale` is the
    image's voxel size in µm (`physical` False when the spacing is not
    known: then no level pools planes). Run here for a mask on this machine
    and by a data node for one it holds -- the same function, the same
    bytes (surface/nets.py)."""
    import polars as pl

    shapes = labels_volume.level_shapes()
    spacing = scale if physical else None
    keep = None
    if table is not None:
        keep = table if spec.ids is None else table.filter(pl.col("label").is_in(list(spec.ids)))
        if spec.ids is not None and keep.height == 0:
            raise SurfaceRefused("none of those ids is an object of this mask")
        counts, faces = keep["volume_vox"].to_numpy(), _faces(keep)
    else:
        stage("scanning")
        found, counts = label_surfaces.label_counts(labels_volume, ids=spec.ids,
                                                    progress=report, cancelled=cancelled)
        faces = None
        if not len(found):
            raise SurfaceRefused("the mask has no objects" if spec.ids is None
                                 else "none of those ids is an object of this mask")
    level, estimate = levels.plan_labels(labels_volume, counts, faces=faces,
                                         quality=spec.quality, level=spec.level,
                                         image_width=image_width, scale=spacing)
    height_k, width_k = shapes[level]
    fx, fy = labels_volume.width / max(1, width_k), labels_volume.height / max(1, height_k)
    # The planner's own rule, so the build pools the planes it was sized for.
    stride = levels.label_z_stride((image_width or shapes[0][1]) / max(1, width_k), spacing,
                                   labels_volume.depth)
    grid = marching.Grid.of((-(-labels_volume.depth // stride), height_k, width_k),
                            factor_xy=(fx, fy), z_stride=stride)
    regions = None if keep is None else label_surfaces.regions_from_table(
        keep, factor_xy=(fx, fy), z_stride=stride, shape=grid.shape)
    stage("extracting")
    surface, rep = label_surfaces.extract_labels(
        labels_volume, level, grid, ids=spec.ids, regions=regions, scale=scale,
        smoothing=mesh_mod.Smoothing.preset(spec.smoothing), inset=spec.inset * min(fx, fy),
        progress=report, stage=stage, cancelled=cancelled, z_stride=stride, threads=threads)
    if not surface.n_triangles:
        raise SurfaceRefused("no object of the mask produced a surface" if spec.ids is None
                             else "none of those ids is an object of this mask")
    return surface, level, {"plan": {"level": level, "grid": grid.to_dict(), "z_stride": stride,
                                     "estimate": int(estimate)},
                            "report": rep, "ids_from": "labels"}


def _objects_frame(project, spec, columns):
    import polars as pl

    frame = pl.DataFrame({k: np.asarray(v) for k, v in columns.items()})
    if spec.type != "label_surfaces":
        return frame
    table = object_table(project, spec.t)
    if table is None:
        return frame
    voxel = {"label": "object", "volume_vox": "voxel_count"}
    for column in ("volume_um3", "surface_area_um2", "sphericity"):
        if column in table.columns:
            voxel[column] = f"voxel_{column}"
    picked = table.select([c for c in voxel if c in table.columns]).rename(voxel)
    return frame.join(picked.with_columns(pl.col("object").cast(pl.Int64)), on="object",
                      how="left")


def write_surface(folder: Path, surface, frame, *, project, spec, level, volume, scale,
                  physical, info, cache_key=None) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    mesh_name = f"mesh-L{level}.bin"
    header = {"level": level, "dims": [volume.width, volume.height, volume.depth],
              "units": "um" if physical else "voxel"}
    lod = mesh_mod.write(surface, folder / mesh_name, header=header)
    tmp = folder / (OBJECTS + ".tmp")
    frame.write_parquet(tmp)
    tmp.replace(folder / OBJECTS)
    from plexora import __version__

    spacing = volume.spacing
    manifest = {
        "version": MANIFEST_VERSION,
        "type": spec.type,
        "label": spec.default_label(),
        "source": {"kind": "channel" if spec.type == "isosurface" else "labels",
                   "channel": spec.channel, "t": spec.t,
                   "image": _provenance(project, "image"),
                   **({"labels": _provenance(project, "segmentation")}
                      if spec.type == "label_surfaces" else {}),
                   **({"built_on": f"node:{_builds_on(project, spec).node}"}
                      if _builds_on(project, spec) is not None else {}),
                   "objects": list(spec.ids) if spec.ids is not None else "all"},
        "params": spec.to_dict(),
        "smoothing": mesh_mod.Smoothing.preset(spec.smoothing).to_dict(),
        "spacing": {"x": spacing.x.to_dict(), "y": spacing.y.to_dict(), "z": spacing.z.to_dict(),
                    "units": "um" if physical else "voxel", "scale": list(scale)},
        "dims": [volume.width, volume.height, volume.depth],
        "algorithm": LABEL_ALGORITHM if spec.type == "label_surfaces" else ALGORITHM,
        "lods": [{"level": level, "path": mesh_name, **lod}],
        "objects": {"count": surface.n_objects, "ids_from": info.get("ids_from"),
                    "touching_bounds": int(np.asarray(frame["touches_bounds"]).sum())
                    if "touches_bounds" in frame.columns else 0},
        "build": info,
        "created_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "plexora_version": __version__,
        "cache_key": cache_key,
        "notes": _notes(spec, physical),
    }
    tmp = folder / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, default=_jsonable), encoding="utf-8")
    tmp.replace(folder / MANIFEST)
    return manifest


def _jsonable(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


def _notes(spec, physical) -> list:
    notes = []
    if spec.type == "isosurface":
        notes.append("An isosurface is the boundary at one chosen value, not a validated "
                     "object boundary.")
    else:
        notes.append("Each surface is extracted from its object's voxels; the mask is "
                     "unchanged and remains the measurement of record.")
    if spec.smoothing != "none":
        notes.append("Smoothed meshes are for display and shape description; voxel "
                     "measurements are the quantitative ones.")
    if not physical:
        notes.append("The z step is not known, so sizes are in voxels.")
    return notes


def register() -> None:
    from plexora.server.models import layer_jobs

    layer_jobs.register_builder(MODALITY, build_layer, stages=STAGES)


# -- after the build: reading, editing, importing ----------------------------------


def current_mesh(layer):
    """The surface's mesh as drawn now (its last version), or None."""
    manifest = read_manifest(layer)
    lods = (manifest or {}).get("lods") or []
    if not lods:
        return None, manifest
    return mesh_mod.read(Path(layer.src) / lods[-1]["path"]), manifest


def project_scale(project):
    """`(scale, physical)` of the project's volume: its spacing as the file and
    the project state it (a z step somebody typed included). Opened and closed
    here -- never the viewer's loaded project, which an agent call must not
    touch (tests/test_agent_architecture.py). A data node's image answers
    from its description."""
    try:
        source = open_volume_for(project)
    except Exception:
        source = None
    if source is None:
        return (1.0, 1.0, 1.0), False
    try:
        return _scale(source.volume)
    finally:
        source.close()


def frame_for(project, layer, surface, manifest):
    """The objects table of `surface` (a version of `layer`)."""
    scale, physical = project_scale(project)
    dims = (manifest or {}).get("dims")
    columns = mesh_mod.measure(surface, scale=scale, physical=physical,
                               dims=tuple(dims) if dims else None)
    spec_raw = (layer.extra or {}).get("surface") or {}
    if spec_raw.get("type") == "label_surfaces":
        return _objects_frame(project, SurfaceSpec.from_dict(spec_raw), columns)
    import polars as pl

    return pl.DataFrame({k: np.asarray(v) for k, v in columns.items()})


def create_imported(project, surface, *, label, units, file_name, given=None, render=None):
    """Register an imported mesh as a ready surface layer: written straight
    into its folder (there is nothing to build), measured as a mesh only."""
    from plexora.server.models.project import LayerSpec, Project

    data = mesh_mod.to_bytes(surface)
    token = "imp" + hashlib.sha256(data).hexdigest()[:9]
    layer_id = layer_id_for(token)
    folder = folder_for(project.name, token)
    folder.mkdir(parents=True, exist_ok=True)
    level = 0
    info = mesh_mod.write(surface, folder / f"mesh-L{level}.bin", header={"imported": True})
    scale, physical = project_scale(project)
    image = project.image
    dims = [int(image.width or 0), int(image.height or 0), int(image.depth or 1)]
    columns = mesh_mod.measure(surface, scale=scale, physical=physical, dims=tuple(dims))
    import polars as pl

    frame = pl.DataFrame({k: np.asarray(v) for k, v in columns.items()})
    frame.write_parquet(folder / OBJECTS)
    from plexora import __version__

    manifest = {
        "version": MANIFEST_VERSION, "type": "imported", "label": label,
        "source": {"kind": "file", "file": file_name, "units": units,
                   "given": given or None},
        "params": {"type": "imported", "units": units},
        "spacing": {"units": "um" if physical else "voxel", "scale": list(scale)},
        "dims": dims, "algorithm": None,
        "lods": [{"level": level, "path": f"mesh-L{level}.bin", **info}],
        "objects": {"count": surface.n_objects, "ids_from": "file",
                    "touching_bounds": int(np.asarray(frame["touches_bounds"]).sum())},
        "created_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "plexora_version": __version__,
        "notes": ["An imported mesh: placed by its units and offset, measured as a mesh only "
                  "(it has no voxels in this image)."],
    }
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2, default=_jsonable),
                                   encoding="utf-8")
    bag = {"color": "#b7a1e0", "style": "solid", "material": "soft", "opacity": 1.0}
    if isinstance(render, dict):
        bag.update(render)
    layer = LayerSpec(id=layer_id, kind=KIND, label=label, src=str(folder), modality=MODALITY,
                      width=dims[0] or None, height=dims[1] or None, render=bag,
                      extra={"surface": {"type": "imported", "file": file_name, "units": units}})
    saved = Project.mutate(project.name, lambda current: current.with_layer(layer))
    if saved is None:
        raise SurfaceRefused(f"{project.name} no longer exists")
    return {"layer": layer_id, "status": "ready", "created": True,
            "objects": surface.n_objects, "triangles": surface.n_triangles}
