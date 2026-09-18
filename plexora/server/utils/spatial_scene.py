"""What a spatial store holds, and where each piece sits.

`resolve_image_path` answers "which single image do I open" for a store that
holds one. This answers the question a scene asks instead: what is in here, and
how do the pieces line up against each other. It is the last step of the layer
model -- by the time anything calls it, every consumer already exists, and the
importer only fills in a model that is already proven.

Two sources, one shape out:

**A SpatialData store.** Elements under `images/`, `labels/`, `points/`,
`shapes/`, each declaring the coordinate systems it lands in. Read as plain JSON,
without importing `spatialdata` -- the metadata IS JSON, and a core build must
not grow a dependency to read it. See `ngff_transform`.

**A Xenium run directory.** A fixed set of filenames beside each other:
`morphology.ome.tif`, `transcripts.parquet`, `cell_boundaries.parquet`,
`nucleus_boundaries.parquet`, `experiment.xenium`. Everything is in MICRONS in a
common frame, so the transform is the reference image's pixel size -- which the
experiment file states.

What comes out is a list of `LayerSpec`s ready for `Project.with_layer`, with the
FIRST image chosen as the reference and every other element's transform composed
through it. Nothing is opened, nothing is converted, and no pixel is read: a
scene with 300 GB of morphology in it is enumerated in milliseconds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from plexora.server.models.project import LayerSpec, normalize_transform
from plexora.server.utils import ngff_transform

#: The SpatialData element groups, and the layer kind each maps to. `labels` are
#: a raster of cell ids -- the same thing Plexora's segmentation mask is -- and
#: `shapes` are polygons, which is boundaries. One line each, because the kind
#: system is a rendering strategy and these are exactly four of them.
SPATIALDATA_KINDS = {
    "images": "image",
    "labels": "labels",
    "points": "points",
    "shapes": "shapes",
}

#: What a Xenium run directory is made of. Filename -> (layer id, kind).
#: Recognised by name because Xenium's output layout is fixed; a directory
#: missing all of them is simply not a Xenium run.
XENIUM_FILES = {
    "morphology.ome.tif": ("morphology", "image"),
    "morphology_focus.ome.tif": ("morphology", "image"),
    "morphology_mip.ome.tif": ("morphology_mip", "image"),
    "transcripts.parquet": ("transcripts", "points"),
    "cell_boundaries.parquet": ("cell_boundaries", "shapes"),
    "nucleus_boundaries.parquet": ("nucleus_boundaries", "shapes"),
}


@dataclass(frozen=True)
class SceneElement:
    """One thing found in a store, before it becomes a layer."""

    id: str
    kind: str
    path: Path
    #: Where this element lands in the shared coordinate system, or None when it
    #: does not declare one. None is information: it means nobody registered
    #: this against anything, which the Layer Manager says out loud.
    to_system: tuple[float, ...] | None = None
    label: str = ""


def is_spatialdata_store(path) -> bool:
    """Whether this directory is laid out the way SpatialData writes one."""
    root = Path(path)
    if not root.is_dir():
        return False
    return any((root / group).is_dir() for group in SPATIALDATA_KINDS)


def is_xenium_run(path) -> bool:
    """Whether this directory is a Xenium output bundle.

    By the experiment file OR by two of the known outputs sitting together: a
    user who copied three files out of a run still has a Xenium run as far as
    this is concerned, and refusing it for a missing manifest would be pedantry.
    """
    root = Path(path)
    if not root.is_dir():
        return False
    if (root / "experiment.xenium").is_file():
        return True
    return sum(1 for name in XENIUM_FILES if (root / name).is_file()) >= 2


def read_spatialdata_scene(path, system="global") -> list[SceneElement]:
    """Every element in a SpatialData store, with its transform into `system`.

    Sorted so the result is stable: two runs over the same store must produce
    the same layer order, or a re-import silently restacks somebody's scene.
    """
    root = Path(path)
    found = []
    for group, kind in SPATIALDATA_KINDS.items():
        directory = root / group
        if not directory.is_dir():
            continue
        for element in sorted(p for p in directory.iterdir() if p.is_dir()):
            attrs = ngff_transform.read_attrs(element)
            found.append(SceneElement(
                id=f"{group}/{element.name}",
                kind=kind,
                path=element,
                to_system=ngff_transform.element_transform(attrs, system),
                label=element.name.replace("_", " "),
            ))
    return found


def read_xenium_scene(path) -> list[SceneElement]:
    """Every output of a Xenium run that Plexora can draw.

    No transforms are read: a Xenium run is already in one frame, in microns, so
    what registers the pieces against each other is the morphology image's pixel
    size -- and that belongs to the caller, which is the only thing that knows
    whether this is the project's reference image or a layer beside one.
    """
    root = Path(path)
    found = []
    for name, (layer_id, kind) in XENIUM_FILES.items():
        candidate = root / name
        if candidate.is_file():
            found.append(SceneElement(
                id=layer_id, kind=kind, path=candidate,
                label=layer_id.replace("_", " ")))
    # Stable order, and images first so `reference_of` picks one.
    order = {"image": 0, "labels": 1, "shapes": 2, "points": 3}
    return sorted(found, key=lambda e: (order.get(e.kind, 9), e.id))


def read_scene(path, system="global") -> list[SceneElement]:
    """Whichever kind of store this is, as elements."""
    if is_spatialdata_store(path):
        return read_spatialdata_scene(path, system)
    if is_xenium_run(path):
        return read_xenium_scene(path)
    return []


def reference_of(elements) -> SceneElement | None:
    """Which element the rest are registered against.

    The first IMAGE, because that is what the viewer draws in the pixel grid of
    and what every stored ROI is already expressed in. A scene with no image at
    all has no reference and every element is unregistered -- which is honest,
    and is the state a pure-transcript sample is genuinely in.
    """
    for element in elements:
        if element.kind == "image":
            return element
    return None


def xenium_pixel_size(path) -> float | None:
    """Microns per pixel, from a Xenium run's own manifest.

    Read rather than assumed. 0.2125 is the published value for the current
    instrument and it is NOT hardcoded here: a run from a different instrument
    or a re-binned export states its own, and a viewer that assumed one would put
    every transcript at the wrong distance from the origin while looking right.
    """
    manifest = Path(path) / "experiment.xenium"
    if not manifest.is_file():
        return None
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for key in ("pixel_size", "pixel_size_um", "um_per_pixel"):
        value = doc.get(key)
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def layers_for(elements, *, reference=None, pixel_size=None) -> list[LayerSpec]:
    """The elements as registered layers, minus the reference itself.

    The reference is excluded because `Project.reference_layer` synthesizes it
    from `ImageSpec` -- storing a second copy under its own id would be a layer
    written and never drawn, which is why `with_layer` refuses the reserved ids.

    Each transform is `inverse(reference -> system)` after `(element -> system)`,
    which is the whole of registration and is why the shared system never has to
    be the one the viewer draws in. An element that does not reach that system
    gets None, and the Layer Manager says "aligned by assumption".

    `pixel_size` covers the Xenium case, where nothing declares a transform and
    the registration IS the conversion from microns to reference pixels.
    """
    reference = reference or reference_of(elements)
    back = None
    if reference is not None and reference.to_system is not None:
        back = ngff_transform.invert(reference.to_system)

    scale = None
    if pixel_size:
        step = 1.0 / float(pixel_size)
        scale = (step, 0.0, 0.0, step, 0.0, 0.0)

    layers = []
    for element in elements:
        if reference is not None and element.id == reference.id:
            continue
        transform = None
        if back is not None and element.to_system is not None:
            transform = ngff_transform.compose(back, element.to_system)
        elif scale is not None:
            transform = scale
        layers.append(LayerSpec(
            id=element.id,
            kind=element.kind,
            label=element.label or element.id,
            src=str(element.path),
            transform=normalize_transform(transform),
            coordinate_system="global" if element.to_system is not None else None,
        ))
    return layers


def register_scene(project, path, *, system="global", pixel_size=None):
    """Add every element of a store to a project as a layer.

    Returns the updated project; the caller saves. Re-registering the same store
    replaces each layer in place rather than appending, because `with_layer`
    keeps a layer's position when it already exists -- so a re-import after a
    corrected pixel size does not restack somebody's scene.
    """
    elements = read_scene(path, system)
    if not elements:
        return project
    if pixel_size is None and is_xenium_run(path):
        pixel_size = xenium_pixel_size(path) or (project.image.pixel_size or {}).get("value")
    for layer in layers_for(elements, pixel_size=pixel_size):
        project = project.with_layer(layer)
    return project
