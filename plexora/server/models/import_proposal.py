"""What is in these files, and what sample they would make.

The engine under **Import Sample**. Somebody points at files or folders; this
says what each one is, which of them belong together, which is the coordinate
system, how the rest line up against it, and what -- if anything -- it could not
work out. Nothing is written and nothing is opened past a header: a Xenium run
with 300 GB of morphology in it is proposed in milliseconds, and a proposal that
is then thrown away has cost a few stats.

**A question is a failure of detection.** Everything here has a default, and
every default is applied whether or not the question is answered: a question is
rendered under the row it concerns so the user can correct a guess, and skipping
it records `unresolved` on the layer rather than refusing the import. That is
the same rule `DataSpec.unresolved` already follows, applied per layer.

The ladder, in order, per picked path:

1. **Bundles.** A folder that is a Xenium run, a SpatialData store, a Visium
   output, a DICOM slide folder or an OME-Zarr image store. A bundle is ONE
   sample and its elements are its layers, which is the whole reason "select a
   folder" is the primary action.
2. **Single files, by content.** An image (by the same sniff quick view used), a
   label mask (by dtype and plane count, with a name hint only as a tie-break),
   a table, a transcript parquet (by its columns), GeoJSON.
3. **Plugin detectors.** `register_detector(fn)`, called after core's ladder and
   before a path is called unrecognised -- so the next vendor format is a file
   in a plugin and nothing changes here.

Then grouping (a bundle is a sample; loose files sharing a stem are a sample),
reference selection (the first raster image, preferring the bundle's own), and
alignment (from the store, from the run's calibration, from pixel size on both
sides, or assumed and said out loud).

What this module is NOT: it does not register anything. `import_sample.py` takes
a proposal and writes it, which is what keeps "what would this be" testable
without a data root.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from plexora.server.utils import spatial_scene

#: Suffixes that are an image before anything else looks at them. Not the whole
#: list of what Plexora reads -- `_sniff_quick_view_kind` owns that -- but the
#: set this module tries the image ladder on at all.
IMAGE_SUFFIXES = (".tif", ".tiff", ".qptiff", ".svs", ".ndpi", ".scn", ".mrxs",
                  ".svslide", ".dcm", ".png", ".jpg", ".jpeg")

#: Suffixes that are a feature table.
TABLE_SUFFIXES = (".csv", ".tsv", ".txt", ".h5ad")

#: Name fragments that say "this is a segmentation". A TIE-BREAK only: the
#: dtype and plane count decide, and this is consulted when they are consistent
#: with both readings (a single-plane uint8 image). A file called `mask.tif`
#: that is three-channel RGB is not a mask, whatever it is called.
MASK_HINTS = ("mask", "label", "seg", "cellpose", "mesmer", "nuclei", "cells")

#: Stem suffixes stripped before grouping loose files. `slide.ome.tif`,
#: `slide_mask.tif` and `slide.csv` are one sample; the mask's name is a
#: statement about the slide, not a different slide.
GROUPING_SUFFIXES = ("_mask", "_masks", "_seg", "_segmentation", "_labels",
                     "_label", "_cells", "_nuclei", "_transcripts", "_quant",
                     "_data")

#: Which raster image a sample prefers as its reference, most preferred first.
#: A Xenium run's own morphology beats an H&E somebody added beside it: every
#: transcript in the run is already expressed in the morphology image's frame,
#: and making the H&E the reference would mean registering the entire run
#: against it to draw anything.
REFERENCE_PREFERENCE = ("xenium_morphology", "multiplex", "he", "picture")

#: Plugin-contributed detectors, in registration order. Called after core's
#: ladder and before a path is reported as unrecognised.
_DETECTORS: list = []


def register_detector(fn) -> None:
    """Add a detector for a format core does not read.

    `fn(path, ctx) -> list[LayerProposal] | None`. None means "not mine", which
    is the answer for almost every path, so a detector must be cheap and must
    not raise on a file it does not recognise.

    The same shape as `layer_jobs.register_builder`, and for the same reason: a
    CosMx or MERSCOPE reader should be a file in a plugin and no change at all
    in core's importer. Nothing registers one today -- the hook exists so that
    the first one does not have to come with a refactor.
    """
    if fn is None:
        raise ValueError("register_detector needs a callable.")
    _DETECTORS.append(fn)


# -- the documents ---------------------------------------------------------

@dataclass
class Question:
    """Something detection could not work out, asked where it applies.

    `scope` is `sample` or `layer:<id>`, which is what puts the control under
    the row it is about instead of in a form of its own. `default` is applied
    whether or not it is answered -- a question never blocks the import.
    """

    id: str
    label: str
    options: tuple = ()
    default: Any = None
    scope: str = "sample"
    kind: str = "choice"
    required: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id, "label": self.label, "scope": self.scope,
            "kind": self.kind, "default": self.default,
            "required": self.required,
            "options": [dict(option) for option in self.options],
        }


@dataclass
class LayerProposal:
    """One row of the import screen: a thing found, and what it would become.

    `role` is what it becomes in the RECORD, which is not the same as `kind`:
    the first raster image is the sample's `ImageSpec` (`role="image"`), the
    first label image is its `SegmentationSpec` (`role="mask"`), a feature table
    is its `DataSpec` (`role="table"`), and everything else is a `LayerSpec`
    (`role="layer"`). Four roles rather than one because those four are stored
    in four different places, and pretending otherwise is what would make the
    importer write layers the viewer does not draw.
    """

    id: str
    kind: str = "image"
    label: str = ""
    src: str | None = None
    modality: str | None = None
    detail: str = ""
    role: str = "layer"
    reference: bool = False
    transform: tuple | None = None
    transform_source: str | None = None
    pixel_size: float | None = None
    geometry: Mapping[str, Any] | None = None
    channels: tuple = ()
    table: str | None = None
    #: Question ids this row is waiting on. Written to `LayerSpec.unresolved`
    #: at registration for the ones still unanswered, so a skipped question is
    #: recorded rather than forgotten.
    needs: tuple = ()
    #: `{install: "pip install ..."}` for a reader this environment has not got.
    #: The row is still proposed: what is missing is a package, not the data.
    dependency: Mapping[str, Any] | None = None
    bundle: Mapping[str, Any] | None = None
    binding: Mapping[str, Any] | None = None
    render: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label,
            "src": self.src, "modality": self.modality, "detail": self.detail,
            "role": self.role, "reference": self.reference,
            "transform": list(self.transform) if self.transform else None,
            "transformSource": self.transform_source,
            "pixelSize": self.pixel_size,
            "geometry": dict(self.geometry) if self.geometry else None,
            "channels": list(self.channels),
            "table": self.table,
            "needs": list(self.needs),
            "dependency": dict(self.dependency) if self.dependency else None,
            "bundle": dict(self.bundle) if self.bundle else None,
            "binding": dict(self.binding) if self.binding else None,
            "render": dict(self.render),
        }


@dataclass
class SampleProposal:
    """One sample the picked paths would make."""

    name: str
    layers: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    bundles: list = field(default_factory=list)
    #: `{width, height, pixel_size}` when there is no raster image and the
    #: sample needs a blank reference frame. None when an image is the frame.
    frame: Mapping[str, Any] | None = None
    #: The name of a project that already holds this data, so the screen offers
    #: "Open the existing sample" rather than making a second copy.
    existing: str | None = None
    dataset: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "layers": [layer.to_dict() for layer in self.layers],
            "questions": [q.to_dict() for q in self.questions],
            "bundles": [dict(b) for b in self.bundles],
            "frame": dict(self.frame) if self.frame else None,
            "existing": self.existing,
            "dataset": self.dataset,
        }


@dataclass
class Proposal:
    samples: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    #: Paths nothing recognised, with the reason. Reported rather than raised:
    #: one unreadable file among five must not refuse the other four.
    unrecognised: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "samples": [sample.to_dict() for sample in self.samples],
            "warnings": list(self.warnings),
            "unrecognised": [dict(entry) for entry in self.unrecognised],
        }

    @property
    def importable(self) -> bool:
        return any(sample.layers for sample in self.samples)


# -- describing ------------------------------------------------------------

def _count(value) -> str:
    """A number a biologist reads rather than one a computer prints."""
    value = int(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return f"{value:,}"


def _dimensions(geometry) -> str:
    if not geometry:
        return ""
    width, height = geometry.get("width"), geometry.get("height")
    return f"{width:,}×{height:,} px" if width and height else ""


def describe(layer: LayerProposal) -> str:
    """The one line under a row's name.

    What a person needs to recognise their own data and notice a mistake: the
    size, the channel count, the molecule count, the calibration. Deliberately
    not a manifest -- everything else is on the edit page, and a row that
    listed twelve facts would be read by nobody.
    """
    bits = []
    named = {
        "xenium_morphology": "Xenium morphology",
        "he": "H&E slide",
        "multiplex": "Multiplex image",
        "picture": "Picture",
        "mask": "Segmentation mask",
        "transcripts": "Transcripts",
        "cell_boundaries": "Cell boundaries",
        "nucleus_boundaries": "Nucleus boundaries",
        "visium_spots": "Visium spots",
        "blank": "Blank frame",
    }
    head = named.get(layer.modality or "")
    if head is None:
        head = {"image": "Image", "labels": "Label image",
                "points": "Points", "shapes": "Shapes",
                "table": "Cell table"}.get(layer.kind, "Layer")
    bits.append(head)

    channels = len(layer.channels or ())
    if layer.kind == "image" and channels > 1:
        bits.append(f"{channels} channels")
    # Only for the kinds where the dimensions are what the user recognises. A
    # points layer's geometry is its EXTENT, read so the blank frame can be
    # sized; printing it beside "48.2M molecules" says nothing and reads as
    # though the transcripts were an image.
    if layer.kind in ("image", "labels"):
        size = _dimensions(layer.geometry)
        if size:
            bits.append(size)
    extra = (layer.render or {}).get("detail")
    if extra:
        bits.append(extra)
    if layer.pixel_size:
        bits.append(f"{layer.pixel_size:g} µm/px")
    return " · ".join(bit for bit in bits if bit)


# -- reading what a file is ------------------------------------------------

def _pixel_size_of(path, geometry=None):
    """Microns per pixel from the file's own metadata, or None.

    Never a default. A calibration nobody stated is absent, and inventing a
    conventional value produces a scale bar that is wrong and looks exactly
    like one that is right -- the rule `normalize_pixel_size` already states.
    """
    from plexora.server.utils import brightfield, dicom_wsi, ome_zarr

    try:
        if ome_zarr.is_zarr_image_path(path):
            payload = ome_zarr.physical_metadata(ome_zarr.open_image(path))
        elif dicom_wsi.is_dicom_path(path):
            payload = dicom_wsi.physical_metadata(dicom_wsi.open_image(path))
        else:
            payload = brightfield.physical_metadata(path)
    except Exception:
        return None
    try:
        value = float((payload or {}).get("physical_size_x"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _points_extent(path):
    """How far a point file reaches, as a geometry, or None.

    Read from the parquet's row-group statistics rather than its rows, so this
    costs a footer read even on a 6 GB file. It exists for one case and it
    matters there: a sample with no image gets a BLANK reference frame, and
    that frame's size is the coordinate system every other layer is registered
    against -- an arbitrary square would put every transcript in the corner of
    a viewer that could not zoom to them.

    The numbers are the file's own units -- microns, for every vendor that
    writes these -- so what comes out is a frame at one micron per pixel unless
    something else states a calibration.
    """
    bounds = spatial_scene.parquet_bounds(path)
    if bounds is None:
        return None
    top_x, top_y = bounds
    return {"width": int(top_x) + 1, "height": int(top_y) + 1,
            "maxLevel": None, "tileWidth": None, "tileHeight": None,
            "numChannels": 0}


def _geometry_of(path, rgb=False):
    """The file's shape, or None when it cannot be read.

    `local.image_geometry`, which is the same reader the node side uses, so a
    slide proposed here and one proposed from a node describe themselves
    identically. Never `convertOmeTiff`: that CONVERTS, and inspection must not.
    """
    from plexora.server.providers import local

    try:
        found = local.image_geometry(path, rgb=rgb)
    except Exception:
        return None
    return {
        "width": found.get("width"),
        "height": found.get("height"),
        "maxLevel": found.get("levels"),
        "tileWidth": found.get("tile_width"),
        "tileHeight": found.get("tile_height"),
        "numChannels": found.get("num_channels"),
    }


def looks_like_label_image(path):
    """Whether this image file is a segmentation mask. `True`, `False` or None.

    None is the answer that matters: it means the file is consistent with BOTH
    readings and the user has to say. That is a single-plane 8-bit image with
    nothing in its name to go on -- which is both a small mask and a grayscale
    photograph, and guessing wrong either way is bad in a way the user cannot
    see (a mask drawn as a channel is a grey square; an image read as a mask is
    a cell-id lookup over a photograph).

    The one place in this module that reads pixels, and it reads one bounded
    window rather than the file: a mask's values are sparse integer ids and an
    image's are a continuum, which shows up in a 512x512 corner.
    """
    import numpy as np

    from plexora.server.utils import ome_zarr

    path = Path(path)
    if path.suffix.lower() in (".png", ".jpg", ".jpeg"):
        return False
    try:
        if ome_zarr.is_zarr_image_path(path):
            return None
        import tifffile as tf

        with tf.TiffFile(str(path), is_ome=False) as handle:
            series = handle.series[0]
            shape = tuple(int(d) for d in series.shape)
            dtype = np.dtype(series.dtype)
            planes = shape[0] if len(shape) > 2 else 1
            if len(shape) > 2 and shape[-1] in (3, 4) and len(shape) == 3:
                # Interleaved colour: three samples of one picture, never a
                # mask. Tested before the plane count, which would read the
                # samples as planes.
                return False
            if planes > 1:
                return False
            if dtype.kind not in "ui":
                return False
            if dtype.itemsize >= 2:
                # 16- or 32-bit, one plane, integer. Nothing else is written
                # that way: a single-channel 16-bit IMAGE exists, but it is
                # almost always one plane of a stack, and a lone one is a mask
                # often enough that the name hint below settles the rest.
                if _name_says_mask(path):
                    return True
                page = handle.pages[0]
                window = page.asarray()[:512, :512]
                unique = int(np.unique(window).size)
                # Ids are sparse; intensities are not. A 512x512 window of a
                # mask holds a few hundred distinct values at most, and one of
                # an image holds thousands.
                return unique < 2048
            return True if _name_says_mask(path) else None
    except Exception:
        return None


def _name_says_mask(path) -> bool:
    stem = Path(path).name.lower()
    return any(hint in stem for hint in MASK_HINTS)


def _install_hint(error):
    """The install line an exception carries, if it carries one.

    Duck-typed rather than caught by class, because the three that have one --
    `BrightfieldSupportMissing`, `DicomSupportMissing`,
    `TranscriptDependencyMissing` -- live in three modules and one of them is a
    plugin's.
    """
    line = getattr(type(error), "INSTALL", None) or getattr(error, "install", None)
    return {"install": str(line)} if line else None


# -- bundles ---------------------------------------------------------------

def _bundle_record(root, fmt, label):
    return {"id": f"{fmt}:{Path(root).name}", "format": fmt,
            "root": str(root), "label": label}


def _xenium_bundle(root):
    """A Xenium run as layers, a table and a calibration."""
    root = Path(root)
    pixel_size = spatial_scene.xenium_pixel_size(root)
    manifest = {}
    experiment = root / "experiment.xenium"
    if experiment.is_file():
        try:
            manifest = json.loads(experiment.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    label = (manifest.get("run_name") or manifest.get("region_name")
             or root.name)
    bundle = _bundle_record(root, "xenium", f"Xenium run · {label}")

    layers = []
    for element in spatial_scene.read_xenium_scene(root):
        proposal = LayerProposal(
            id=element.id, kind=element.kind, modality=element.modality,
            label=element.label.title() if element.kind != "image" else "Morphology",
            src=str(element.path), bundle=bundle,
            # Everything in a Xenium run is in microns in one frame, so what
            # registers the pieces against each other is the morphology image's
            # own pixel size -- stated by the run rather than assumed.
            transform_source="run",
            pixel_size=pixel_size if element.kind == "image" else None,
        )
        if element.kind == "image":
            proposal.role = "image"
            proposal.geometry = _geometry_of(element.path)
            proposal.channels = _channel_stubs(element.path, proposal.geometry)
        elif element.kind == "points":
            peek = spatial_scene.peek_parquet(element.path)
            if peek is None:
                proposal.dependency = {
                    "install": 'pip install "plexora[spatial]"'}
                proposal.render = {"detail": "install pyarrow to read it"}
            else:
                proposal.render = {"detail": f"{_count(peek['rows'])} molecules"}
                proposal.geometry = _points_extent(element.path)
        elif element.kind == "shapes":
            peek = spatial_scene.peek_parquet(element.path)
            if peek:
                proposal.render = {"detail": f"{_count(peek['rows'])} rows"}
        proposal.detail = describe(proposal)
        layers.append(proposal)

    for role, table_path in spatial_scene.xenium_tables(root):
        if role == "cells":
            peek = spatial_scene.peek_parquet(table_path)
            table = LayerProposal(
                id="cells", kind="table", role="table", modality="cells",
                label="Cells", src=str(table_path), bundle=bundle,
                table="parquet",
                render={"detail": f"{_count(peek['rows'])} cells"} if peek else {})
            table.detail = describe(table)
            layers.append(table)
        else:
            # Recorded on the row and NOT read. Saying so is the point: a run
            # imported with its expression matrix silently ignored is a sample
            # whose marker tools are mysteriously empty.
            matrix = LayerProposal(
                id="expression", kind="table", role="note", modality="expression",
                label="Expression matrix", src=str(table_path), bundle=bundle,
                detail="Cell × gene matrix · recorded, not read yet")
            layers.append(matrix)

    return layers, [], bundle, pixel_size


def _channel_stubs(path, geometry):
    """Channel entries for a layer, named the way the tile route parses them.

    `{name, fullname}` only -- `src` is filled at REGISTRATION, because it
    names the project this layer ends up in and this module does not know it.
    The trailing `_<N>` is load-bearing: `data_model._parse_channel` reads the
    index back out of it, and a name without one reads as a label mask.
    """
    count = int((geometry or {}).get("numChannels") or 0)
    if count <= 0:
        return ()
    from plexora.server.models.data_model import _image_channel_stem

    stem = _image_channel_stem(Path(path))
    return tuple({"name": f"{stem}_{index}", "fullname": f"{stem}_{index}"}
                 for index in range(count))


def _spatialdata_bundle(root, answers):
    """A SpatialData store as layers, a mask, a table and its questions."""
    from plexora.server.models.adapters.spatialdata_adapter import list_spatialdata_tables

    root = Path(root)
    bundle = _bundle_record(root, "spatialdata", f"SpatialData · {root.name}")
    elements = spatial_scene.read_spatialdata_scene(root)
    layers, questions = [], []

    images = [e for e in elements if e.kind == "image"]
    labels = [e for e in elements if e.kind == "labels"]
    chosen = answers.get("reference")
    reference = next((e for e in images if e.id == chosen), None) or (
        images[0] if images else None)
    if len(images) > 1:
        questions.append(Question(
            id="reference", label="Which image is this sample drawn in?",
            kind="select",
            options=tuple({"value": e.id, "label": e.label} for e in images),
            default=reference.id if reference else None))

    for element in elements:
        proposal = LayerProposal(
            id=element.id.replace("/", "_"), kind=element.kind,
            modality=element.modality or None,
            label=element.label.title(), src=str(element.path), bundle=bundle,
            transform_source="store" if element.to_system is not None else "assumed")
        if element.kind == "image":
            proposal.geometry = _geometry_of(element.path)
            proposal.channels = _channel_stubs(element.path, proposal.geometry)
            proposal.pixel_size = _pixel_size_of(element.path)
            if reference is not None and element.id == reference.id:
                proposal.role, proposal.reference = "image", True
        elif element.kind == "labels" and labels and element.id == labels[0].id:
            # The FIRST label element becomes the sample's segmentation, which
            # is the one the viewer draws cell ids from. A second is an
            # ordinary layer -- there is one `imageData[0]`, and two masks
            # cannot both be it.
            proposal.role = "mask"
            proposal.geometry = _geometry_of(element.path)
        proposal.detail = describe(proposal)
        layers.append(proposal)

    try:
        tables = list_spatialdata_tables(root)
    except Exception:
        tables = []
    if tables:
        chosen_table = answers.get("table")
        names = [t["name"] for t in tables]
        table_name = chosen_table if chosen_table in names else names[0]
        table = LayerProposal(
            id="table", kind="table", role="table", modality="cells",
            label="Cells", src=str(root), table=table_name,
            bundle=bundle,
            render={"detail": f"table {table_name}"})
        if len(tables) > 1:
            questions.append(Question(
                id="table", scope="layer:table",
                label="Which table holds the cells?", kind="select",
                options=tuple({"value": t["name"], "label": t["name"]}
                              for t in tables),
                default=table_name))
            if not chosen_table:
                # Answered by default so the import proceeds, and RECORDED as
                # outstanding so the layer says which question was skipped.
                table.needs = ("table",)
        table.detail = describe(table)
        layers.append(table)

    return layers, questions, bundle, None


def _visium_bundle(root):
    """A Visium run: the hires picture as the frame, spots over it."""
    root = Path(root)
    bundle = _bundle_record(root, "visium", f"Visium · {root.name}")
    factors = spatial_scene.visium_scalefactors(root) or {}
    scale = factors.get("tissue_hires_scalef")
    layers, warnings = [], []

    for element in spatial_scene.read_visium_scene(root):
        proposal = LayerProposal(
            id=element.id, kind=element.kind, modality=element.modality,
            label=element.label, src=str(element.path), bundle=bundle)
        if element.kind == "image":
            # A PNG that is the REFERENCE of a multi-layer sample is registered
            # as a tiled brightfield image, not as `rgb`. `image_kind == "rgb"`
            # boots RgbImageViewer, which has no layer stack and no plugins --
            # so a Visium sample registered that way could not draw its own
            # spots. See C7 in the plan.
            proposal.role, proposal.reference = "image", True
            proposal.modality = "he"
            proposal.geometry = _geometry_of(element.path, rgb=True)
            proposal.render = {"rgb": True, "tiled": True}
        else:
            # Spot centres are recorded in the FULL-resolution slide's pixels;
            # the hires picture is `tissue_hires_scalef` of it. Getting this
            # wrong puts every spot off by a factor of about six while looking
            # entirely plausible.
            if scale:
                proposal.transform = (float(scale), 0.0, 0.0, float(scale), 0.0, 0.0)
                proposal.transform_source = "run"
            else:
                proposal.transform_source = "assumed"
            radius = factors.get("spot_diameter_fullres")
            proposal.render = {"pointKind": "spot"}
            if radius:
                proposal.render["radius"] = float(radius) / 2.0 * float(scale or 1)
        proposal.detail = describe(proposal)
        layers.append(proposal)

    matrix = next(iter(sorted(root.glob("*feature_bc_matrix.h5"))), None)
    if matrix is not None:
        warnings.append(
            f"{matrix.name} is recorded but not read yet -- Plexora draws the "
            "spots and does not load Visium expression values.")
    return layers, [], bundle, warnings


def _zarr_image_candidates(root):
    """Every OME-Zarr image inside a store, as `(id, path)`.

    A listing rather than `resolve_image_path`'s single answer, because the
    import screen ASKS when there is more than one instead of raising. Written
    beside that function rather than under it: its error messages are what a
    Python-API caller sees and are pinned by tests, and it is a different
    question -- "which one" versus "which ones are there".
    """
    from plexora.server.utils import ome_zarr

    root = Path(root)
    if ome_zarr._is_multiscale(root):
        return [(root.name, root)]
    fields = ome_zarr._plate_fields(root)
    if fields:
        return [(field_, root / field_) for field_ in fields]
    series = sorted((name for name in ome_zarr._zarr_children(root)
                     if name.isdigit()), key=int)
    numbered = [(name, root / name) for name in series
                if ome_zarr._is_multiscale(root / name)]
    if numbered:
        return numbered
    images = root / "images"
    if images.is_dir():
        return [(name, images / name) for name in ome_zarr._zarr_children(images)]
    return []


# -- the ladder ------------------------------------------------------------

def _detect(path, answers):
    """What one picked path is. `(layers, questions, bundle, warnings)`."""
    path = Path(path)
    if not path.exists():
        return [], [], None, [f"{path} does not exist."]

    if path.is_dir():
        return _detect_directory(path, answers)
    return _detect_file(path, answers)


def _detect_directory(path, answers):
    from plexora.server.utils import dicom_wsi, ome_zarr

    if spatial_scene.is_xenium_run(path):
        layers, questions, bundle, _ = _xenium_bundle(path)
        return layers, questions, bundle, []
    if spatial_scene.is_visium_run(path):
        layers, questions, bundle, warnings = _visium_bundle(path)
        return layers, questions, bundle, warnings
    if spatial_scene.is_spatialdata_store(path):
        layers, questions, bundle, _ = _spatialdata_bundle(path, answers)
        return layers, questions, bundle, []
    if dicom_wsi.is_dicom_path(path):
        return _dicom_slides(path, answers)
    if ome_zarr.is_zarr_image_path(path):
        return _zarr_images(path, answers)

    # A plain folder. One level of recognised files rather than a recursive
    # walk: somebody who points at their home directory should get "nothing
    # here", not a five-minute scan.
    found, questions, warnings = [], [], []
    for child in sorted(path.iterdir()):
        if child.is_dir():
            continue
        layers, child_questions, _, child_warnings = _detect_file(child, answers)
        found.extend(layers)
        questions.extend(child_questions)
        warnings.extend(child_warnings)
    if not found:
        return [], [], None, []
    return found, questions, None, warnings


def _dicom_slides(path, answers):
    from plexora.server.utils import dicom_wsi

    try:
        dicom_wsi.assemble_slide(path)
    except Exception as error:
        hint = _install_hint(error)
        if hint:
            layer = LayerProposal(
                id=_layer_id(path), kind="image", role="image", reference=True,
                modality="he", label=Path(path).name, src=str(path),
                dependency=hint, needs=("install",),
                detail="DICOM slide · needs an extra package")
            return [layer], [], None, []
        return [], [], None, [f"{Path(path).name}: {error}"]
    layer = LayerProposal(
        id=_layer_id(path) or "slide", kind="image", role="image",
        reference=True, modality="he", label=Path(path).name, src=str(path))
    layer.geometry = _geometry_of(path)
    layer.channels = _channel_stubs(path, layer.geometry)
    layer.pixel_size = _pixel_size_of(path)
    layer.detail = describe(layer)
    return [layer], [], None, []


def _zarr_images(path, answers):
    candidates = _zarr_image_candidates(path)
    if not candidates:
        return [], [], None, [
            f"{Path(path).name} is a zarr store with no OME-Zarr image in it."]
    chosen = answers.get("image")
    picked = next((c for c in candidates if c[0] == chosen), candidates[0])
    questions = []
    if len(candidates) > 1:
        questions.append(Question(
            id="image", label="Which image?", kind="select",
            options=tuple({"value": name, "label": name}
                          for name, _ in candidates),
            default=picked[0]))
    layer = LayerProposal(
        id=picked[0], kind="image", role="image", reference=True,
        modality="multiplex", label=picked[0], src=str(picked[1]))
    layer.geometry = _geometry_of(picked[1])
    layer.channels = _channel_stubs(picked[1], layer.geometry)
    layer.pixel_size = _pixel_size_of(picked[1])
    layer.detail = describe(layer)
    return [layer], questions, None, []


def _detect_file(path, answers):
    """One file, by content. `(layers, questions, bundle, warnings)`."""
    suffix = path.suffix.lower()

    if suffix == ".parquet":
        return _detect_parquet(path)
    if suffix in (".geojson", ".json") and _is_feature_collection(path):
        layer = LayerProposal(
            id=_layer_id(path), kind="shapes", modality="annotations",
            label=path.stem.replace("_", " ").title(), src=str(path),
            detail="Shapes · registered; drawing them is not built yet")
        return [layer], [], None, []
    if suffix in IMAGE_SUFFIXES:
        return _detect_image(path, answers)
    if suffix in TABLE_SUFFIXES:
        return _detect_table(path)
    for detector in _DETECTORS:
        try:
            found = detector(path, {"answers": dict(answers)})
        except Exception:
            found = None
        if found:
            return list(found), [], None, []
    return [], [], None, []


def _detect_parquet(path):
    if spatial_scene.is_xenium_transcripts(path):
        peek = spatial_scene.peek_parquet(path)
        layer = LayerProposal(
            id="transcripts", kind="points", modality="transcripts",
            label="Transcripts", src=str(path),
            geometry=_points_extent(path),
            render={"detail": f"{_count(peek['rows'])} molecules"} if peek else {})
        layer.detail = describe(layer)
        return [layer], [], None, []
    if spatial_scene.looks_like_cells_parquet(path):
        peek = spatial_scene.peek_parquet(path)
        layer = LayerProposal(
            id="cells", kind="table", role="table", modality="cells",
            label="Cells", src=str(path), table="parquet",
            render={"detail": f"{_count(peek['rows'])} cells"} if peek else {})
        layer.detail = describe(layer)
        return [layer], [], None, []
    peek = spatial_scene.peek_parquet(path)
    if peek is None:
        return [], [], None, [
            f"{path.name}: reading parquet needs pyarrow "
            '(pip install "plexora[spatial]").']
    return [], [], None, [
        f"{path.name} is a parquet Plexora does not recognise "
        f"(columns: {', '.join(peek['columns'][:6])})."]


def _is_feature_collection(path) -> bool:
    """Whether this JSON is GeoJSON, by its first few hundred bytes.

    A prefix read rather than a parse: an exported annotation set is tens of
    megabytes, and `"type": "FeatureCollection"` is in the first line of every
    one of them.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            head = handle.read(400)
    except OSError:
        return False
    return "FeatureCollection" in head


def _detect_image(path, answers):
    from plexora.datasource import _sniff_quick_view_kind
    from plexora.server.providers import local

    verdict = looks_like_label_image(path)
    answer = answers.get(f"mask-or-image:{path.name}")
    if answer:
        verdict = answer == "mask"
    questions = []
    if verdict is None:
        questions.append(Question(
            id=f"mask-or-image:{path.name}",
            label=f"Is {path.name} a segmentation mask or an image?",
            options=({"value": "image", "label": "An image"},
                     {"value": "mask", "label": "A segmentation mask"}),
            default="image"))
        verdict = False

    if verdict:
        layer = LayerProposal(
            id=_layer_id(path), kind="labels", role="mask", modality="mask",
            label="Segmentation mask", src=str(path),
            geometry=_geometry_of(path))
        layer.needs = tuple(q.id for q in questions)
        layer.detail = describe(layer)
        return [layer], questions, None, []

    try:
        kind = _sniff_quick_view_kind(path)
    except Exception as error:
        hint = _install_hint(error)
        if hint is None:
            return [], questions, None, [f"{path.name}: {error}"]
        layer = LayerProposal(
            id=_layer_id(path), kind="image", role="image", reference=True,
            label=path.name, src=str(path), dependency=hint,
            needs=("install",),
            detail="Whole-slide image · needs an extra package")
        return [layer], questions, None, []

    detection = None
    try:
        detection = local.detect_image_type(path)
    except Exception:
        detection = None
    brightfield_like = bool(getattr(detection, "verdict", None) == "brightfield")
    rgb = kind == "rgb" or brightfield_like

    layer = LayerProposal(
        id=_layer_id(path), kind="image", role="image", reference=True,
        label=path.name, src=str(path),
        modality=("picture" if kind == "rgb"
                  else ("he" if brightfield_like else "multiplex")))
    layer.geometry = _geometry_of(path, rgb=rgb)
    layer.channels = _channel_stubs(path, layer.geometry)
    layer.pixel_size = _pixel_size_of(path)
    if rgb:
        layer.render = {"rgb": True}
    layer.needs = tuple(q.id for q in questions)
    layer.detail = describe(layer)
    return [layer], questions, None, []


def _looks_like_a_table(path) -> bool:
    """Whether a text file has a header that could be a table's.

    `detect_data_type` dispatches on the SUFFIX, which is right for the field
    somebody typed a path into -- they said it was their data -- and wrong for
    a folder scan, where `.txt` is mostly a readme. So a delimited-text file is
    checked here before it is proposed: one line read, and it has to have at
    least two columns under one delimiter.

    Only for the delimited formats. `.h5ad` is a container, not text, and its
    suffix genuinely does say what it is.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            header = handle.readline()
    except OSError:
        return False
    return any(header.count(delimiter) >= 1 for delimiter in (",", "	", ";"))


def _detect_table(path):
    from plexora.server.models.adapters import detect_data_type

    if path.suffix.lower() in (".csv", ".tsv", ".txt") and not _looks_like_a_table(path):
        return [], [], None, [
            f"{path.name} has no delimited header — it does not look like "
            "a table."]
    try:
        data_type = detect_data_type(path)
    except ValueError as error:
        return [], [], None, [str(error)]
    layer = LayerProposal(
        id="table", kind="table", role="table", modality="cells",
        label="Cells", src=str(path), table=None,
        render={"detail": data_type})
    layer.detail = describe(layer)
    return [layer], [], None, []


# -- grouping --------------------------------------------------------------

def _layer_id(path) -> str:
    """A layer id from a filename, without the format's own extensions.

    `slide.ome.tif` is the slide called "slide", not one called "slide.ome" --
    and the id is what appears in tile urls, in `ctx.layers.find` and on the
    card, so it is worth being the name a person would use.
    """
    return _clean_name(Path(path).name.split(".", 1)[0]) or "layer"


def _group_stem(path) -> str:
    """The stem loose files are grouped by.

    `slide.ome.tif`, `slide_mask.tif` and `slide.csv` are one sample: the
    mask's name is a statement ABOUT the slide, not a different slide.
    """
    name = Path(path).name
    # `.ome.tif` and friends: take everything before the first dot, so
    # `slide.ome.tif` and `slide.csv` agree.
    stem = name.split(".", 1)[0].lower()
    for suffix in GROUPING_SUFFIXES:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _sample_name(paths, bundles) -> str:
    """What to call this sample, before the user edits it.

    The bundle's own label first -- a Xenium run knows its run name -- then the
    shared stem of the files, then the first file's stem. Never a generated id:
    a name the user recognises is what makes the library usable.
    """
    if bundles:
        root = Path(bundles[0]["root"])
        return _clean_name(root.name)
    stems = {_group_stem(p) for p in paths}
    if len(stems) == 1 and stems != {""}:
        return _clean_name(next(iter(stems)))
    return _clean_name(Path(paths[0]).stem if paths else "sample")


def _clean_name(value) -> str:
    value = re.sub(r"[^\w\-. ]+", "_", str(value or "").strip())
    return value or "sample"


def _align(layer, reference, reference_pixel_size, run_pixel_size):
    """Put one layer into the reference layer's pixel grid.

    In order: what the store declared, what the run's calibration implies, the
    ratio of two stated pixel sizes, and -- when nothing says -- nothing, which
    the Layer Manager already reports as "aligned by assumption". Deliberately
    NOT a guess dressed as a measurement: a layer placed by an invented scale
    looks registered and is wrong everywhere, which is the one failure
    registration exists to prevent.
    """
    if layer.transform is not None or layer.transform_source == "store":
        return layer
    if layer.transform_source == "run" and run_pixel_size:
        # Xenium: everything is in microns and the reference image is
        # `run_pixel_size` microns across per pixel, so microns -> pixels is
        # one over it.
        step = 1.0 / float(run_pixel_size)
        layer.transform = (step, 0.0, 0.0, step, 0.0, 0.0)
        layer.transform_source = "run"
        return layer
    if layer.pixel_size and reference_pixel_size:
        ratio = float(layer.pixel_size) / float(reference_pixel_size)
        layer.transform = (ratio, 0.0, 0.0, ratio, 0.0, 0.0)
        layer.transform_source = "pixel_size"
        return layer
    layer.transform = None
    layer.transform_source = "assumed"
    return layer


def _frame_for(layers):
    """A blank reference frame big enough for what this sample holds.

    For a sample with no raster image: transcripts on their own, a table and a
    mask, spots. The extent comes from whatever states one -- a mask's own
    geometry first, because it is measured -- and the calibration from the
    run's manifest when there is one.
    """
    for layer in layers:
        if layer.geometry and layer.geometry.get("width"):
            return {"width": int(layer.geometry["width"]),
                    "height": int(layer.geometry["height"]),
                    "pixel_size": layer.pixel_size}
    pixel_size = next((l.pixel_size for l in layers if l.pixel_size), None)
    # Nothing measured anything. A frame still has to have a size, and this one
    # is honest about being a placeholder: registration replaces it the moment
    # a layer's build reports real bounds.
    return {"width": 1024, "height": 1024, "pixel_size": pixel_size}


def _pick_reference(layers):
    """Which raster image the rest are registered against."""
    images = [l for l in layers if l.kind == "image" and l.role == "image"]
    if not images:
        images = [l for l in layers if l.kind == "image"]
    if not images:
        return None
    def rank(layer):
        try:
            preference = REFERENCE_PREFERENCE.index(layer.modality or "")
        except ValueError:
            preference = len(REFERENCE_PREFERENCE)
        area = ((layer.geometry or {}).get("width") or 0) * (
            (layer.geometry or {}).get("height") or 0)
        return (preference, -area)
    return sorted(images, key=rank)[0]


def _find_existing(reference, layers):
    """A project that already holds this data, or None."""
    from plexora import get_config
    from plexora.datasource import _find_existing_datasource_for_image
    from plexora.server.models.project import Project

    config = get_config() or {}
    if reference is not None and reference.src:
        found = _find_existing_datasource_for_image(reference.src, config)
        if found:
            return found
    roots = {str(Path(l.bundle["root"]).resolve()) for l in layers
             if l.bundle and l.bundle.get("root")}
    if not roots:
        return None
    for name, entry in config.items():
        project = Project.from_entry(name, entry)
        for bundle in project.bundles:
            if str(Path(bundle.get("root", "")).resolve()) in roots:
                return name
    return None


# -- the entry point -------------------------------------------------------

def inspect_paths(paths, *, node=None, answers=None, sample=None) -> Proposal:
    """What these paths hold, and what sample they would make.

    @param answers - question id -> the user's answer, applied on this pass.
        Re-inspecting with answers is how the screen narrows: a store's table
        chosen, a mask-or-image settled, a grouping decided.
    @param sample - an existing project's name, for "+ Add Layer": the
        reference is that project's image, its already-registered sources are
        dropped, and nothing about naming or datasets is proposed.
    """
    answers = dict(answers or {})
    paths = [p for p in (paths or []) if str(p).strip()]
    proposal = Proposal()
    if not paths:
        return proposal

    if node:
        return _inspect_on_node(paths, node, answers, sample)

    found, questions, bundles, warnings = [], [], [], []
    for raw in paths:
        layers, path_questions, bundle, path_warnings = _detect(raw, answers)
        if not layers:
            proposal.unrecognised.append({
                "path": str(raw),
                "reason": (path_warnings[0] if path_warnings else
                           "Nothing Plexora can read here."),
            })
            continue
        found.extend(layers)
        questions.extend(path_questions)
        warnings.extend(path_warnings)
        if bundle is not None and bundle not in bundles:
            bundles.append(bundle)

    proposal.warnings.extend(warnings)
    if not found:
        return proposal

    if sample:
        proposal.samples.append(_scoped_sample(sample, found, questions, bundles))
        return proposal

    for group in _split_samples(found, bundles, questions, answers, proposal):
        proposal.samples.append(group)
    return proposal


def _split_samples(found, bundles, questions, answers, proposal):
    """One sample, or several, out of what was detected.

    A bundle is always one sample. Loose files sharing a stem are one sample.
    Several raster images with different stems is the only genuinely ambiguous
    case, and it is the one question asked -- defaulting to separate samples,
    because two slides in a folder are usually two slides.
    """
    if bundles:
        return [_assemble(_sample_name([b["root"] for b in bundles], bundles),
                          found, questions, bundles)]

    images = [l for l in found if l.kind == "image" and l.role == "image"]
    stems = {_group_stem(l.src) for l in found if l.src}
    grouping = answers.get("images-grouping") or "separate"
    if len(images) > 1 and len(stems) > 1:
        questions = list(questions) + [Question(
            id="images-grouping",
            label=f"{len(images)} images. Import as separate samples, or as "
                  "layers of one?",
            options=({"value": "separate",
                      "label": f"{len(images)} samples"},
                     {"value": "layers", "label": "One sample, N layers"}),
            default="separate")]
        if grouping == "separate":
            samples = []
            for image in images:
                stem = _group_stem(image.src)
                mine = [l for l in found if l.src and _group_stem(l.src) == stem]
                samples.append(_assemble(_clean_name(stem), mine,
                                         questions, []))
            # Anything that matched no image's stem rides with the first
            # sample rather than being dropped: a table named after the study
            # is still that study's table.
            claimed = {id(l) for sample in samples for l in sample.layers}
            orphans = [l for l in found if id(l) not in claimed]
            if orphans and samples:
                samples[0].layers.extend(orphans)
                _finish(samples[0])
            return samples

    return [_assemble(_sample_name([l.src for l in found if l.src], bundles),
                      found, questions, bundles)]


def _assemble(name, layers, questions, bundles):
    sample = SampleProposal(name=_clean_name(name), layers=list(layers),
                            questions=list(questions),
                            bundles=[dict(b) for b in bundles])
    _finish(sample)
    return sample


def _finish(sample):
    """Choose the reference, align everything to it, and fill in the frame."""
    reference = _pick_reference(sample.layers)
    run_pixel_size = next(
        (l.pixel_size for l in sample.layers
         if l.transform_source == "run" and l.pixel_size), None)
    if reference is None:
        run_pixel_size = run_pixel_size or next(
            (l.pixel_size for l in sample.layers if l.pixel_size), None)

    for layer in sample.layers:
        layer.reference = layer is reference
        if layer is reference:
            layer.role = "image"
            layer.transform = None
            layer.transform_source = None
            continue
        if layer.role in ("table", "note"):
            continue
        if layer.role == "image":
            # A second raster image is a LAYER, not a second ImageSpec. One
            # sample has one coordinate system, and this is the line where a
            # picked file stops being a candidate for it.
            layer.role = "layer"
        _align(layer, reference,
               reference.pixel_size if reference else None,
               run_pixel_size)

    if reference is None:
        sample.frame = _frame_for(sample.layers)
        # Everything is registered against a frame whose calibration is the
        # data's own, so a layer that was going to be scaled by a ratio is now
        # already in place.
        for layer in sample.layers:
            if layer.transform_source == "pixel_size":
                layer.transform, layer.transform_source = None, "assumed"
    else:
        sample.frame = None

    # Asked only once the layers are settled, because until then it is not
    # known which file would be the reference.
    sample.existing = _find_existing(reference, sample.layers)
    for layer in sample.layers:
        layer.detail = layer.detail or describe(layer)


def _scoped_sample(name, found, questions, bundles):
    """"+ Add Layer": everything is a layer of a sample that already exists."""
    from plexora.server.models.project import Project

    try:
        project = Project.load(name)
    except KeyError:
        project = None

    registered = {str(Path(l.src).resolve()) for l in (project.spatial_layers if project else ())
                  if l.src}
    if project is not None and project.image.src:
        registered.add(str(Path(project.image.src).resolve()))

    layers = []
    reference_pixel_size = ((project.image.pixel_size or {}).get("value")
                            if project else None)
    run_pixel_size = next((l.pixel_size for l in found
                           if l.transform_source == "run" and l.pixel_size), None)
    for layer in found:
        try:
            if layer.src and str(Path(layer.src).resolve()) in registered:
                continue
        except OSError:
            pass
        if layer.role == "image":
            # There is already a reference. A picked image joins the scene as a
            # registered layer; promoting it is a separate, explicit act on its
            # card, because it would re-express every other layer's transform.
            layer.role = "layer"
            layer.reference = False
        if layer.role not in ("table", "note", "mask"):
            _align(layer, None, reference_pixel_size, run_pixel_size)
        layers.append(layer)

    sample = SampleProposal(name=name, layers=layers, questions=list(questions),
                            bundles=[dict(b) for b in bundles])
    return sample


def _inspect_on_node(paths, node, answers, sample):
    """The same questions, answered by the machine that can open the files.

    A node is the only process that can read its own filesystem, so what comes
    back is the node's own inspection reshaped into this document -- not a
    second implementation. Bundles are not proposed from here: enumerating a
    run directory needs a directory listing this side does not have, and the
    honest answer is to say so rather than to half-detect it.
    """
    from plexora import nodes as node_api

    proposal = Proposal()
    layers = []
    for resource_id in paths:
        try:
            document = node_api.inspect_table(node, resource_id,
                                              table=answers.get("table"))
        except Exception as error:
            proposal.unrecognised.append(
                {"path": str(resource_id), "reason": str(error)})
            continue
        layer = LayerProposal(
            id="table", kind="table", role="table", modality="cells",
            label="Cells", src=None,
            binding={"node": node, "resource_id": str(resource_id)},
            table=document.get("table"),
            render={"detail": document.get("data_type") or ""})
        layer.detail = describe(layer)
        layers.append(layer)
    if layers:
        proposal.samples.append(_assemble(
            sample or _clean_name(Path(str(paths[0])).stem), layers, [], []))
    return proposal
