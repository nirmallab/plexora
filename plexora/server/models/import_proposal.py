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
   folder" is the primary action. A Xenium, Visium or Visium HD run is spatial
   transcriptomics, a Pro root: on a licence without it the run is not
   detected at all (`_licensed`), and neither is a loose transcripts parquet.
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
import threading
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from plexora.server.providers.base import is_remote_locator
from plexora.server.utils import boundary_mask, ngff_transform, spatial_scene

#: Suffixes that are an image before anything else looks at them. Not the whole
#: list of what Plexora reads -- `_sniff_quick_view_kind` owns that -- but the
#: set this module tries the image ladder on at all.
IMAGE_SUFFIXES = (".tif", ".tiff", ".qptiff", ".svs", ".ndpi", ".scn", ".mrxs",
                  ".svslide", ".dcm", ".png", ".jpg", ".jpeg", ".ims")

#: Suffixes that are a feature table. `.parquet` is NOT here: it is dispatched
#: before this list, by its columns, because the same extension carries a cell
#: table, a transcript table and a boundary table (see `_detect_parquet`).
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

#: The column that makes a delimited file a channel list rather than a table
#: of cells. mcmicro's `markers.csv` is the common one -- `channel_number,
#: cycle_number,marker_name`, one row per image channel -- and it sits at the
#: root of a run, shared by every slide in it. Read as the panel, never as
#: data: one row per channel is not one row per cell.
MARKER_SHEET_COLUMN = "marker_name"

#: Filenames that are a channel list even without that column: a one-column
#: `markers.csv`, the older mcmicro spelling.
MARKER_SHEET_FILES = ("markers.csv",)

#: How many folder levels below a picked folder a segmentation mask is looked
#: for when none sits beside its images. Pipelines file masks a level or two
#: down -- mcmicro's `segmentation/unmicst-<sample>/cell.ome.tif`, a folder per
#: segmenter -- and four covers what they write.
MASK_SEARCH_DEPTH = 4

#: How many entries that search looks at before giving up. A run holds tens of
#: files; a pick of somebody's home directory holds a great many more, and the
#: honest answer there is "no mask found" rather than a minute of stats.
MASK_SEARCH_LIMIT = 1000

#: Words in a path that say a segmentation lives there, for a candidate whose
#: pixels cannot settle it on their own (a window of pure background). Broader
#: than `MASK_HINTS`, which judges a file somebody picked: this only ever
#: judges a file that already has an image's exact shape.
DEEP_MASK_HINTS = MASK_HINTS + ("cell", "nucle", "segment")

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


#: Which licence entitlement each 10x run layout needs to be detected. The
#: spatial-transcriptomics modality is a Pro root (plexora/licensing/
#: manifest.py), and not granted means not shown: without it these runs are
#: not proposed at all -- no layers, no bundle, and no warning naming a
#: licence. A project already imported from one still opens; this gates only
#: making a new one.
XENIUM_ENTITLEMENT = "transcriptomics:xenium"
VISIUM_ENTITLEMENT = "transcriptomics:visium"


def _licensed(entitlement) -> bool:
    """Whether this licence grants `entitlement`. Never raises.

    Asked per inspection rather than once at import, so a licence activated in
    Settings is honoured by the next pick. Imported lazily, like every other
    caller of licensing: this module is loaded by the CLI and the notebook API,
    and licensing misbehaving reads as not granted, never as a broken importer.
    """
    try:
        from plexora import licensing

        return bool(licensing.allows(entitlement))
    except Exception:
        return False


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
    #: Which of the caller's picks asked it, by position -- so a question
    #: about one run folder goes on that run's card and no other. Internal.
    pick: int | None = None
    #: The `src` of the row it is about, when a pick holds several samples'
    #: rows: a folder of two slides asks "which mask?" of one of them, and
    #: `pick` alone would draw it on both cards. Internal.
    about: str | None = None

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
    #: The name of the file this came from, when `src` cannot carry it. A
    #: resource on a node is addressed by an id derived from its path, and an
    #: id is exactly what naming and grouping must not read (see
    #: `_sample_name`): a slide and its mask are one sample because their
    #: FILENAMES say so. The name and not the path, because the path is on
    #: another machine -- resolving one here would either find nothing or,
    #: worse, find this machine's own file of the same name. Kept out of
    #: `to_dict`: the row's label already says which file this is.
    filename: str | None = None
    #: Which of the caller's picks this row came out of, by POSITION in the
    #: list it sent. A folder yields several rows carrying the same index,
    #: which is the point: removing any of them removes the folder.
    #:
    #: The screen cannot work this out for itself. A pick is a path and a row
    #: carries a `src`, and the two differ every time something is resolved on
    #: the way -- a node rewrites `~/slide.tif` into `node://o2/<derived id>`,
    #: and a local pick is expanded and normalised. Comparing those strings is
    #: what made Remove silently do nothing for a remote image.
    pick: int | None = None
    #: The `key` of the sample this pick was added TO, when it was added by a
    #: card's own "+ Add segmentation mask" rather than picked loose. Resolved
    #: in `inspect_paths` from the answer key `sample-for:<pick>`, so a row
    #: carries it whatever its filename turns out to be. Internal: the screen
    #: already knows, since it said so.
    attach: str | None = None
    #: The pick itself, as the caller spelled it -- what every per-pick answer
    #: (`sample-for:`, `added-as:`) is filed under (see `_said`). Carried so a
    #: question asked about this row later is asked under the same key the
    #: card's own action would have used. Internal, like `attach`.
    said: str | None = None
    #: `"fullres"` when `transform` is stated in a Visium run's full-resolution
    #: microscope frame rather than in the reference's pixels. `_align`
    #: composes it with the reference's `frameScale` (the hires PNG is a
    #: fraction of that frame) and clears it, so finishing twice cannot scale
    #: twice. Internal, like `attach`.
    frame: str | None = None
    #: The image this row was found FOR, by its `src`, when its own name says
    #: nothing: `mccellpose-LSP11385/cell.ome.tif` is LSP11385's mask because
    #: of the folder it sits in, and grouping by its stem ("cell") would put it
    #: nowhere. Internal, like `attach`.
    found_for: str | None = None
    #: Names for the image's channels, in channel order, from a channel list
    #: picked beside it (`markers.csv`). Empty means the image keeps its own.
    #: Internal: the row's detail says where the names came from.
    channel_names: tuple = ()
    #: What this row could be instead, and how to say so: `{question,
    #: options: [{value, label}], value}`. The role badge on the import
    #: screen is a menu over these, and choosing one answers `question` --
    #: the same answer key a `mask-or-image` question uses -- so a confident
    #: detection is as correctable as an uncertain one, without a question in
    #: front of every row. None where there is no other reading (a
    #: multi-channel stack is never a mask; a JPEG cannot hold ids).
    switch: Mapping[str, Any] | None = None
    #: A cell table spanning several images: `{column, values, value}` -- the
    #: obs column that says which image each cell is in, its values, and the
    #: one this sample reads (`_choose_subsets`), or None when nothing names
    #: it. Registration reads only that image's cells, so the table opens
    #: with the sample instead of waiting for somebody to say which.
    subset: Mapping[str, Any] | None = None

    def to_dict(self) -> dict:
        return {
            "pick": self.pick,
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
            "switch": dict(self.switch) if self.switch else None,
            "subset": ({"column": self.subset.get("column"),
                        "value": self.subset.get("value")}
                       if self.subset else None),
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
    #: What this sample IS, derived from its own contents rather than from
    #: where it happens to sit in the list. `stem:lsp11641`, `bundle:<root>`.
    #:
    #: Two things need it. A card's "+ Add segmentation mask" has to say which
    #: sample the file joins, and it has to still mean the same sample after a
    #: re-inspection has reordered or renumbered the list. And registration
    #: sends it back beside the index, so a POST that was composed against an
    #: older look at the files is refused rather than silently registering
    #: whatever is at that position now.
    key: str = ""
    #: The modalities this sample has NOT got, out of `("mask", "table")`, in
    #: that order. The card turns each one into an action -- "+ Add
    #: segmentation mask", "+ Add data" -- so the next step is offered where
    #: the sample is, rather than as another trip through a file picker.
    missing: tuple = ()
    #: The folder that tells this sample apart from another with the same
    #: stem -- two runs' `image.ome.tif` -- so the two get two keys. None for
    #: the ordinary case, where the stem alone is unique. Internal: `key`
    #: already carries it to the screen.
    distinct: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "key": self.key,
            "missing": list(self.missing),
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
        "visium_bins": "Visium HD bins",
        "blank": "Blank frame",
    }
    head = named.get(layer.modality or "")
    if layer.kind == "table" and (layer.render or {}).get("tenx") == "spots":
        head = "Spot table"
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
    from plexora.server.utils import segmentation_pyramid

    if segmentation_pyramid.is_png(path):
        # Only a mask asks: a PNG image is a picture, which has no geometry
        # row. Off the header, one plane, one level.
        header = segmentation_pyramid.png_header(path)
        if header is None:
            return None
        return {"width": header.width, "height": header.height, "maxLevel": 1,
                "tileWidth": None, "tileHeight": None, "numChannels": 1}
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


#: The two readings a single plane of integers has, as the role menu on the
#: import screen offers them (`LayerProposal.switch`).
ROLE_OPTIONS = ({"value": "image", "label": "Image"},
                {"value": "mask", "label": "Segmentation mask"})


def _role_switch(question_id, current):
    """A row's `switch` between image and mask, answered under `question_id`."""
    return {"question": question_id, "value": current,
            "options": [dict(option) for option in ROLE_OPTIONS]}


def _single_plane(geometry) -> bool:
    """Whether a raster has one channel: the only kind that can be a mask."""
    try:
        return int((geometry or {}).get("numChannels") or 1) == 1
    except (TypeError, ValueError):
        return False


def looks_like_label_image(path):
    """Whether this image file is a segmentation mask. `True`, `False` or None.

    None is the answer that matters: it means the file is consistent with BOTH
    readings and the user has to say. Every raster row can still be switched
    between the two on the import screen (`LayerProposal.switch`), so a wrong
    confident answer costs a click rather than a deleted sample -- but a
    question in front of every ambiguous file is still how the likely wrong
    guesses are caught before anybody has to notice them.

    Decided by the PIXELS, and by the name only when the pixels cannot say
    (`_name_says_mask`). It used to be the other way round for any 16-bit
    plane: a filename containing "nuclei" or "seg" made a DAPI channel a mask
    before a pixel was read, and a plane whose top-left corner held a zero and
    fewer than 2048 distinct values -- a dark corner of almost any slide --
    was called a mask with no question asked. See `_window_verdict` for what
    the pixels are asked now.
    """
    import numpy as np

    from plexora.server.utils import ome_zarr, segmentation_pyramid

    path = Path(path)
    # A format that cannot hold cell ids -- a JPEG, a whole-slide container,
    # an Imaris file, a colour PNG -- is an image, and asking about one would
    # be putting a question in front of somebody that has only one answer.
    if segmentation_pyramid.mask_format_refusal(path) is not None:
        return False
    if segmentation_pyramid.is_ims(path):
        # Stains, a channel of ids, or both: asked of the channels themselves
        # (volume/sources/ims.mask_verdict).
        from plexora.server.volume.sources import ims

        return ims.mask_verdict(path)
    if segmentation_pyramid.is_png(path):
        # Greyscale or palette (a colour one was refused above): one plane of
        # 8- or 16-bit integers, asked the same question a TIFF plane is.
        header = segmentation_pyramid.png_header(path)
        if header is None:
            return False
        dtype = np.dtype(np.uint16 if header.bit_depth == 16 else np.uint8)
        try:
            plane = segmentation_pyramid.read_png_labels(path)
            return _plane_verdict(path, dtype, plane.shape[-2:],
                                  lambda rows, cols: plane[rows, cols])
        except Exception:
            return None
    try:
        if ome_zarr.is_zarr_image_path(path):
            return _zarr_label_verdict(path)
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
                # A stack of integer planes can be a 3-D label volume -- one id
                # per cell across every plane it spans (plexora/server/volume/
                # labels.py). Said so by the middle plane looking like ids
                # (or, when that plane cannot say, by its name); a stack of
                # intensities is anything else.
                # A (T, Z, Y, X) stack is one label volume per frame.
                if len(shape) in (3, 4) and dtype.kind in "ui" and _volume_looks_like_labels(
                        handle, path, dtype):
                    return True
                return False
            reader = _tiff_reader(handle)
            return _plane_verdict(path, dtype, shape[-2:], reader)
    except Exception:
        return None


#: The side of one window the label test reads, and how many windows it may
#: read before giving up on a plane that is mostly background.
LABEL_WINDOW = 512
LABEL_WINDOWS = 5


def _window_origins(height, width, size=LABEL_WINDOW):
    """Top-left corners of up to `LABEL_WINDOWS` windows over a plane.

    The centre first, then the centres of the four quadrants -- where the
    tissue is. It used to be the top-left corner alone, which on a slide is
    the one place almost guaranteed to be background: an image's empty corner
    read as "sparse, with zeros", which is what a mask looks like.
    """
    def clamp(value, extent):
        return max(0, min(int(value), max(0, int(extent) - size)))

    centres = [(height / 2, width / 2), (height / 4, width / 4),
               (height / 4, 3 * width / 4), (3 * height / 4, width / 4),
               (3 * height / 4, 3 * width / 4)]
    origins = []
    for row, col in centres:
        origin = (clamp(row - size / 2, height), clamp(col - size / 2, width))
        if origin not in origins:
            origins.append(origin)
    return origins[:LABEL_WINDOWS]


def _tiff_reader(handle, lead=None):
    """`read(rows, cols)` over a TIFF's full-resolution plane, decoding only
    the tiles a window covers.

    `pages[0].asarray()` decodes the WHOLE plane before the slice: three
    seconds and gigabytes for a slide-sized mask, paid on every inspection --
    which a folder scan does for each mask it finds. Through the same lazy
    zarr view the tile route reads, a window is a tile or two. The whole-page
    read is the fallback for a file that view cannot open.
    """
    import numpy as np
    import zarr

    try:
        store = handle.series[0].aszarr(level=0)
        array = zarr.open(store, mode="r")
    except Exception:
        pages = handle.series[0].pages
        plane = pages[0 if lead is None else len(pages) // 2].asarray()
        return lambda rows, cols: plane[rows, cols]
    index = tuple(lead) if lead is not None else (0,) * max(array.ndim - 2, 0)

    def read(rows, cols):
        return np.asarray(array[index + (rows, cols)])

    read.close = store.close
    return read


def _plane_verdict(path, dtype, shape, read):
    """`looks_like_label_image` for one plane of `dtype`: True/False/None.

    `read(rows, cols)` returns a window of the plane; `shape` is its (height,
    width). The pixels decide; the name breaks a tie the pixels leave.
    """
    if dtype.kind not in "ui":
        return False
    try:
        verdict = _windows_verdict(shape, read)
    finally:
        closer = getattr(read, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:
                pass
    if verdict is not None:
        return verdict
    return True if _name_says_mask(path) else None


def _windows_verdict(shape, read):
    """The first decisive verdict among the plane's windows, or None.

    A window that is all background says nothing, so the next is read; one
    that holds enough of the plane to judge decides.
    """
    height, width = (int(shape[0]), int(shape[1]))
    for top, left in _window_origins(height, width):
        window = read(slice(top, top + LABEL_WINDOW),
                      slice(left, left + LABEL_WINDOW))
        verdict = _window_verdict(window)
        if verdict is not None:
            return verdict
    return None


#: Of the neighbouring pixel pairs inside cells, how many must be equal for
#: a plane to read as ids. A cell is one value from edge to edge, so in a
#: mask only the pairs that straddle two touching cells differ -- a few in a
#: hundred at full resolution. Intensities are a continuum: neighbours in a
#: fluorescence channel are almost never exactly equal.
LABEL_SAME_FLOOR = 0.75
#: Below this the plane is an image, whatever else it looks like.
IMAGE_SAME_CEILING = 0.5
#: How much of its bounding box a typical id fills. A cell is a blob, so its
#: pixels fill most of the box around them; one INTENSITY value is scattered
#: over the whole window, or drawn as a thin contour, and fills almost none.
LABEL_FILL_FLOOR = 0.2
IMAGE_FILL_CEILING = 0.08


def _window_verdict(window):
    """Whether one window of an integer plane is label ids. True/False/None.

    Two tests, both needed:

    - **Piecewise constant, both ways.** Among horizontally and vertically
      neighbouring pixels that are both non-zero, the share that are equal --
      the smaller of the two directions, so a gradient that is constant down
      its columns does not pass on them.
    - **Compact.** Each distinct value's pixels fill their own bounding box,
      the way a cell does. A flat region of an image passes the first test;
      it does not pass this one unless it really is shaped like cells.

    None when the window cannot say: almost all background, or a single
    non-zero value (a binary mask and a flat field read the same).
    """
    import numpy as np

    plane = np.asarray(window)
    while plane.ndim > 2:
        plane = plane[0]
    if plane.size < 64:
        return None
    present = plane != 0
    across = present[:, 1:] & present[:, :-1]
    down = present[1:, :] & present[:-1, :]
    pairs_across, pairs_down = int(across.sum()), int(down.sum())
    if min(pairs_across, pairs_down) < 32:
        return None
    same = min(
        int((across & (plane[:, 1:] == plane[:, :-1])).sum()) / pairs_across,
        int((down & (plane[1:, :] == plane[:-1, :])).sum()) / pairs_down)
    if same < IMAGE_SAME_CEILING:
        return False

    rows, cols = np.nonzero(present)
    values = plane[rows, cols]
    order = np.argsort(values, kind="stable")
    values, rows, cols = values[order], rows[order], cols[order]
    starts = np.flatnonzero(np.r_[True, values[1:] != values[:-1]])
    if starts.size < 2:
        return None
    counts = np.diff(np.r_[starts, values.size])
    tall = np.maximum.reduceat(rows, starts) - np.minimum.reduceat(rows, starts) + 1
    wide = np.maximum.reduceat(cols, starts) - np.minimum.reduceat(cols, starts) + 1
    fill = counts / (tall.astype(np.float64) * wide)
    sized = counts >= 4
    if not sized.any():
        return None
    typical = float(np.median(fill[sized]))
    if typical < IMAGE_FILL_CEILING:
        return False
    if same >= LABEL_SAME_FLOOR and typical >= LABEL_FILL_FLOOR:
        return True
    return None


def _zarr_label_verdict(path):
    """`looks_like_label_image` for a zarr store. True/False/None.

    A store usually SAYS: NGFF writes `image-label` into a label image's
    attributes and files it under `labels/`, and an image carries `omero`
    channels. Only a store that says neither has its pixels read -- one
    channel of integers, through the same windows a TIFF plane is.
    """
    import numpy as np

    from plexora.server.utils import ome_zarr, segmentation_pyramid

    try:
        group = ome_zarr._open_group(path)
    except Exception:
        return None
    try:
        attrs = ome_zarr._ome_attrs(group)
    except Exception:
        attrs = {}
    if "image-label" in attrs:
        return True
    if not ome_zarr._is_remote(path) and Path(path).parent.name == "labels":
        return True
    multiscale = ome_zarr._multiscale_of(group) if not hasattr(group, "dtype") else None
    try:
        if hasattr(group, "dtype"):
            array = group
        elif multiscale:
            array = group[ome_zarr._dataset_paths(multiscale)[0]]
        else:
            array = group["0"]
        dtype = np.dtype(array.dtype)
        axes = ome_zarr._axes_of(multiscale or {}, array.ndim)
    except Exception:
        return None
    if dtype.kind not in "ui":
        return False
    if "c" in axes and int(array.shape[axes.index("c")]) > 1:
        return False
    try:
        plane = segmentation_pyramid._zarr_level_zero(group)
        shape = tuple(int(n) for n in array.shape[-2:])
        verdict = _windows_verdict(
            shape, lambda rows, cols: np.asarray(plane[rows, cols]))
    except Exception:
        verdict = None
    if verdict is not None:
        return verdict
    if attrs.get("omero"):
        # Channels with colours and windows are how an IMAGE describes itself.
        return False
    return True if _name_says_mask(path) else None


def _volume_looks_like_labels(handle, path, dtype) -> bool:
    """Whether a (Z, Y, X) integer stack is label ids rather than intensities.

    The middle plane is asked what a single plane is asked; its name only
    breaks a tie the pixels leave.
    """
    shape = tuple(int(d) for d in handle.series[0].shape)
    lead = tuple(int(depth) // 2 for depth in shape[:-2])
    read = None
    try:
        read = _tiff_reader(handle, lead)
        verdict = _windows_verdict(shape[-2:], read)
    except Exception:
        verdict = None
    finally:
        closer = getattr(read, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:
                pass
    if verdict is not None:
        return verdict
    return dtype.itemsize >= 2 and _name_says_mask(path)


def _name_says_mask(path) -> bool:
    stem = Path(path).name.lower()
    return any(hint in stem for hint in MASK_HINTS)


def _focal_note(path) -> str:
    """What a stack's planes will be read as, for the row, or `""`.

    "5 focal planes, opens on the middle one" for a z-stack read as depth
    (plexora/server/volume): every plane can be stepped through, and the row
    says which one the viewer opens on. "4 planes on Z read as imaging cycles"
    for a multi-channel stack whose file states no z step -- the Akoya/CODEX
    convention, folded into channels as it always has been -- so a panel of
    C x Z channels reads as a decision and not as a file Plexora misread.

    A header read, no pixels, and never an error: a file this cannot open is a
    file some other row will already be complaining about.
    """
    try:
        import tifffile as tf

        from plexora.server.volume.sources import tiff as tiff_volume

        with tf.TiffFile(str(path), is_ome=False) as handle:
            series = handle.series[0]
            described = tiff_volume.describe(handle, series, new_import=True)
            found = None if described else tiff_volume.page_layout(handle, series)
    except Exception:
        return ""
    if described is not None:
        volume, _layout = described
        parts = []
        if volume.depth > 1:
            kind = "focal planes" if volume.channels == 1 else "planes"
            step = volume.spacing.z
            apart = (f" {step.value:g} {step.unit} apart" if step.value and step.source == "metadata"
                     else "")
            parts.append(f"{volume.depth} {kind}{apart}, opens on the middle one")
        if volume.frames > 1:
            parts.append(f"{volume.frames} time points")
        return "; ".join(parts)
    if found is not None:
        sizes = tiff_volume.sizes_of(found[0])
        if sizes["z"] > 1 and sizes["c"] > 1:
            return (f"{sizes['z']} planes on Z read as imaging cycles "
                    "(the file states no z step)")
    return ""


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


#: The Xenium element whose polygons become the sample's segmentation mask.
#: Cells rather than nuclei, because the cell is what the feature table counts
#: and what every gate is expressed over -- a nucleus mask would colour a
#: smaller shape by a measurement made over a larger one.
CELL_BOUNDARY_ELEMENT = "cell_boundaries"


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
            # Which of the run's three pictures this is. A run ships a focus
            # composite, a maximum projection and the raw Z-stack of the same
            # tissue; the preference between them lives in `XENIUM_FILES` and
            # the row is where the user gets to check it.
            note = (_focal_note(element.path)
                    or spatial_scene.xenium_image_note(element.path))
            if note:
                proposal.render = {"detail": note}
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
            if (element.id == CELL_BOUNDARY_ELEMENT
                    and boundary_mask.is_boundary_table(element.path)):
                # The run's segmentation, stated as outlines rather than as
                # pixels. Claimed as the sample's mask so the viewer draws it
                # the way it draws every other segmentation -- Outlines,
                # Filled, colour by whatever the table says about each cell.
                # A raster mask the user supplies alongside outranks it; see
                # `import_sample._preferred_mask`.
                proposal.role = "mask"
                proposal.modality = "mask"
                proposal.label = "Cell segmentation"
                if peek:
                    proposal.render = {
                        "detail": f"{_count(peek['rows'])} boundary vertices"}
        proposal.detail = describe(proposal)
        layers.append(proposal)

    for role, table_path in spatial_scene.xenium_tables(root):
        if role == "cells":
            peek = spatial_scene.peek_parquet(table_path)
            table = LayerProposal(
                id="cells", kind="table", role="table", modality="cells",
                label="Cells", src=str(table_path), bundle=bundle,
                # No `table`: that field names a table INSIDE a container, the
                # way a SpatialData store's does. A parquet is the table, and
                # its encoding is `detect_data_type`'s answer -- putting
                # "parquet" here wrote it into `DataSpec.table`, where it read
                # as the name of a table nothing would ever find.
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
        # The ONE question with no default. Everywhere else a guess is applied
        # and shown so it can be corrected; here it cannot be, because reading
        # the wrong table gives you a different set of cells and nothing on
        # screen says so. A store with several tables therefore imports with
        # the path recorded and `unresolved: ("table",)` beside it -- the
        # project opens as an image and the first tool that needs cells asks,
        # which is exactly what `deferred_spec` already does.
        table_name = (chosen_table if chosen_table in names
                      else (names[0] if len(names) == 1 else None))
        table = LayerProposal(
            id="table", kind="table", role="table", modality="cells",
            label="Cells", src=str(root), table=table_name,
            bundle=bundle,
            render={"detail": f"table {table_name}" if table_name
                              else f"{len(names)} tables — which holds the cells?"})
        if len(tables) > 1:
            questions.append(Question(
                id="table", scope="layer:table",
                label="Which table holds the cells?", kind="select",
                options=tuple({"value": t["name"], "label": t["name"]}
                              for t in tables),
                default=table_name))
            if not table_name:
                table.needs = ("table",)
        table.detail = describe(table)
        layers.append(table)

    return layers, questions, bundle, None


#: What a standard Visium run's `spatial/` folder holds besides the hires
#: picture and the positions, and what each one is. The CytAssist pictures are
#: in the CytAssist instrument's own frame, which Space Ranger does not write a
#: transform out of -- so they are recorded, never drawn misplaced over the
#: spots. `aligned_tissue_image.jpg` is in the microscope frame, but it is
#: Space Ranger's REGISTRATION QC -- a checkerboard of alternating CytAssist
#: and microscope squares -- so over the real image it only looked empty.
VISIUM_RECORDED = (
    ("aligned_tissue_image.jpg", "Registration check",
     "Space Ranger's CytAssist-to-microscope alignment checkerboard"),
    ("tissue_lowres_image.png", "Low-res tissue image",
     "the hires picture at a third of the size"),
    ("cytassist_image.tiff", "CytAssist image",
     "the CytAssist instrument's picture, in its own frame"),
    ("aligned_fiducials.jpg", "Fiducial alignment",
     "Space Ranger's fiducial QC, in the CytAssist frame"),
    ("detected_tissue_image.jpg", "Detected tissue",
     "Space Ranger's tissue-detection QC, in the CytAssist frame"),
    ("spatial_enrichment.csv", "Spatial enrichment",
     "Moran's I per feature, from Space Ranger"),
)


def _visium_matrix(root):
    """The run's feature matrix: the filtered one when both are there."""
    found = sorted(Path(root).glob("*feature_bc_matrix.h5"))
    return next((m for m in found if "filtered" in m.name),
                found[0] if found else None)


def _visium_bundle(root):
    """A Visium run: the hires picture as the frame, the spots as its table.

    The spots ARE the rows of the feature matrix, so the matrix becomes the
    sample's table and every tool that colours cells colours spots. They are
    ALSO a `visium_spots` layer -- nothing to build, its values are the
    table's -- whose card holds the Visium panel's gene list, heatmap and
    composition, exactly as a Visium HD run's bins layer does.

    Everything is stated in the run's FULL-RES microscope frame, which the
    hires PNG is `tissue_hires_scalef` of -- the Visium HD bundle's
    convention, so `_align` composes it once whatever the reference is.
    """
    from plexora.server.utils import brightfield, tenx_matrix

    root = Path(root)
    bundle = _bundle_record(root, "visium", f"Visium · {root.name}")
    factors = spatial_scene.visium_scalefactors(root) or {}
    scalef = factors.get("tissue_hires_scalef")
    if factors.get("spot_diameter_fullres"):
        bundle["spotDiameterFullres"] = float(factors["spot_diameter_fullres"])
    spatial = root / spatial_scene.VISIUM_SPATIAL
    layers, warnings = [], []

    picture = next((spatial / name for name in (
        "tissue_hires_image.png", "tissue_lowres_image.png")
        if (spatial / name).is_file()), None)
    if picture is not None:
        scale = (scalef if picture.name.startswith("tissue_hires")
                 else factors.get("tissue_lowres_scalef")) or 1.0
        # Space Ranger writes this PNG from whatever the microscope image
        # was, so it is an H&E only when the slide was: a CytAssist protein
        # run imaged by immunofluorescence gives a false-colour composite on
        # black. `multiplex` registers it as its three channels.
        found = brightfield.detect_picture_type(picture)
        fluorescent = found.verdict == brightfield.FLUORESCENCE
        image = LayerProposal(
            id="tissue_image", kind="image",
            modality="multiplex" if fluorescent else "he", role="image",
            reference=True,
            label="Fluorescence image" if fluorescent else "Tissue image",
            src=str(picture), bundle=bundle,
            geometry=_geometry_of(picture, rgb=True),
            # A PNG that is the REFERENCE of a multi-layer sample is
            # registered as a tiled image, not as `rgb`: `image_kind == "rgb"`
            # boots RgbImageViewer, which has no layer stack and no plugins,
            # so the spots could not be drawn over it. See C7 in the plan.
            render={"rgb": True, "tiled": True, "frameScale": float(scale),
                    "detail": ("an immunofluorescence composite"
                               if fluorescent else "")},
            transform=(1.0 / scale, 0.0, 0.0, 1.0 / scale, 0.0, 0.0),
            transform_source="run", frame="fullres")
        image.detail = describe(image)
        layers.append(image)

    matrix = _visium_matrix(root)
    if matrix is not None:
        try:
            summary = tenx_matrix.matrix_summary(matrix)
        except Exception:
            summary = None
        bits = []
        if summary:
            bits.append(f"{_count(summary['barcodes'])} spots")
            bits.append(f"{summary['genes']:,} features")
        if (spatial / "barcode_fluorescence_intensity.csv").is_file():
            bits.append("IF intensity per spot")
        table = LayerProposal(
            id="cells", kind="table", role="table", modality="cells",
            label="Spot table", src=str(matrix), bundle=bundle,
            render={"detail": " · ".join(bits), "tenx": "spots"})
        table.detail = describe(table)
        layers.append(table)
        # The spots as a LAYER, beside the table they are the rows of: the
        # card the Visium panel lives in (plugins/visium_hd, spotLayer.js),
        # which colours them by gene. Nothing to build -- its values are the
        # table's, read whole, and its positions are stated in the full-res
        # frame like everything else here.
        spots = LayerProposal(
            id="spots", kind="points", modality="visium_spots",
            label="Visium spots", src=str(matrix), bundle=bundle,
            transform=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0), transform_source="run",
            frame="fullres",
            render={"pointKind": "spot",
                    "spotDiameter": factors.get("spot_diameter_fullres"),
                    "detail": f"{_count(summary['barcodes'])} spots · coloured by gene"
                              if summary else "coloured by gene"})
        spots.detail = describe(spots)
        layers.append(spots)
    else:
        warnings.append("This run has no feature matrix, so its spots have "
                        "nothing to be drawn from.")

    for name, label, what in VISIUM_RECORDED:
        path = spatial / name
        if path.is_file():
            layers.append(LayerProposal(
                id=Path(name).stem, kind="image" if path.suffix != ".csv"
                else "table", role="note", label=label, src=str(path),
                bundle=bundle, detail=f"{what} · recorded, not drawn"))
    return layers, [], bundle, warnings


#: Which bin level becomes the sample's table unless the user says otherwise.
#: 8 microns: Space Ranger's own recommendation for analysis, 1.25 million
#: rows on an 11 mm slide -- where every table consumer is interactive. The
#: 2 micron level is 20 million rows, which the viewer draws at every zoom
#: (from the bin store) but which is not yet a table anything should gate on.
VISIUM_HD_DEFAULT_BIN = 8

#: Past this many rows a table row says so. Not a refusal -- it will load --
#: but gating and the cell explorer ship values per row, and 20 million of
#: them are minutes, not a slider.
LARGE_TABLE_ROWS = 5_000_000


def _visium_hd_bundle(root, answers):
    """A Visium HD run: the hires H&E, the bins over it, one table.

    The picture is the reference and everything else is registered against
    the run's FULL-RES microscope frame, which the hires PNG is
    `tissue_hires_scalef` of (see `LayerProposal.frame`): so a full-res H&E
    picked alongside can win the reference and nothing has to be restated.

    The bins are ONE layer at the finest level, whatever level the table is:
    the viewer pools them on the fly, so 2, 8 and 16 micron pictures are one
    store. The table is one level, chosen by the `bin-size` question -- or
    Space Ranger 4's segmented cells, whose polygons then become the mask.
    """
    from plexora.server.utils import tenx_matrix

    levels = spatial_scene.visium_hd_levels(root)
    root = spatial_scene.visium_hd_root(root) or Path(root)
    # Named for the folder the run is in: Space Ranger calls every run's
    # output `outs`, and a library of samples called "outs" is no library.
    named = root
    while named.parent != named and (
            named.name in ("outs", spatial_scene.VISIUM_HD_BINNED)
            or spatial_scene.VISIUM_HD_BIN_RE.match(named.name)):
        named = named.parent
    bundle = _bundle_record(root, "visium_hd", f"Visium HD · {named.name}")
    bundle["name"] = named.name
    finest = levels[0]
    factors = finest.scalefactors or {}
    scalef = factors.get("tissue_hires_scalef")
    microns = factors.get("microns_per_pixel")
    bundle["fullres_pixel_size"] = microns
    layers, questions, warnings = [], [], []

    picture = finest.hires or finest.lowres
    if picture is not None:
        scale = (scalef if picture == finest.hires
                 else factors.get("tissue_lowres_scalef")) or 1.0
        image = LayerProposal(
            id="tissue_image", kind="image", modality="he", role="image",
            reference=True, label="Tissue image", src=str(picture),
            bundle=bundle, geometry=_geometry_of(picture, rgb=True),
            render={"rgb": True, "tiled": True, "frameScale": float(scale)},
            # What registers this picture when a full-res H&E is the
            # reference instead: it is `scale` of that frame. Cleared by
            # `_finish` while it IS the reference.
            transform=(1.0 / scale, 0.0, 0.0, 1.0 / scale, 0.0, 0.0),
            transform_source="run", frame="fullres",
            pixel_size=(float(microns) / float(scale)) if microns else None)
        image.detail = describe(image)
        layers.append(image)

    shape = spatial_scene.visium_hd_grid_shape(finest)
    try:
        transform, residual = spatial_scene.visium_hd_bin_transform(finest)
    except Exception as exc:
        transform, residual = None, None
        warnings.append(f"The bins could not be registered: {exc}")
    summary = None
    if finest.matrix is not None:
        try:
            summary = tenx_matrix.matrix_summary(finest.matrix)
        except Exception:
            summary = None
    bins = LayerProposal(
        id="bins", kind="points", modality="visium_bins",
        label="Visium HD bins", src=str(finest.matrix) if finest.matrix else None,
        bundle=bundle, transform=transform, frame="fullres",
        transform_source="run" if transform else "assumed",
        geometry=({"width": shape[1], "height": shape[0]} if shape else None),
        render={
            "pointKind": "bin",
            "binMicrons": finest.size_um,
            "gridShape": list(shape) if shape else None,
            "micronsPerPixel": microns,
            "positions": str(finest.positions),
            "detail": " · ".join(bit for bit in (
                f"{finest.size_um:g} µm",
                f"{_count(summary['barcodes'])} bins in tissue" if summary else "",
                f"{summary['genes']:,} genes" if summary else "") if bit),
        })
    if residual is not None:
        bins.render["fitResidualPx"] = round(float(residual), 4)
    bins.detail = describe(bins)
    layers.append(bins)

    # -- the table: one level, or the segmented cells --------------------
    segmentation = spatial_scene.visium_hd_segmentation(root)
    by_size = {int(round(level.size_um)): level for level in levels}
    options = []
    # The segmented cells when the run has them: they are the only table the
    # cell polygons can be the mask of (see below), and the 2 µm bins layer
    # still draws expression at every zoom whichever table is chosen.
    cells_default = bool(segmentation and segmentation.get("matrix")
                         and segmentation.get("cells"))
    for size, level in sorted(by_size.items()):
        rows = None
        try:
            rows = tenx_matrix.matrix_summary(level.matrix)["barcodes"]
        except Exception:
            pass
        label = f"{size} µm bins" + (f" · {_count(rows)} rows" if rows else "")
        if size == VISIUM_HD_DEFAULT_BIN and not cells_default:
            label += " (recommended)"
        elif rows and rows > LARGE_TABLE_ROWS:
            label += " — slow to gate"
        options.append({"value": str(size), "label": label, "rows": rows})
    if segmentation and segmentation.get("matrix"):
        try:
            rows = tenx_matrix.matrix_summary(segmentation["matrix"])["barcodes"]
        except Exception:
            rows = None
        options.append({"value": "cells", "label": "Segmented cells" + (
            f" · {_count(rows)} cells" if rows else "")
            + (" (recommended)" if cells_default else ""), "rows": rows})
    default = ("cells" if cells_default
               else str(VISIUM_HD_DEFAULT_BIN) if VISIUM_HD_DEFAULT_BIN in by_size
               else options[0]["value"] if options else None)
    chosen = str(answers.get("bin-size") or default or "")
    if chosen not in {o["value"] for o in options}:
        chosen = default
    if len(options) > 1:
        questions.append(Question(
            id="bin-size", scope="layer:cells",
            label="Which table should the sample analyse?", kind="choice",
            options=tuple({"value": o["value"], "label": o["label"]}
                          for o in options),
            default=default))

    for option in options:
        value = option["value"]
        if value == "cells":
            matrix, table_label = segmentation["matrix"], "Segmented cells"
        else:
            level = by_size[int(value)]
            matrix, table_label = level.matrix, f"{value} µm bins"
        if matrix is None:
            continue
        rows = option.get("rows")
        if value == chosen:
            detail = (f"{_count(rows)} {'cells' if value == 'cells' else 'bins'}"
                      if rows else "")
            if summary:
                detail += f" · {summary['genes']:,} genes"
            table = LayerProposal(
                id="cells", kind="table", role="table", modality="cells",
                label=table_label, src=str(matrix), bundle=bundle,
                render={"detail": detail.strip(" ·"),
                        "tenx": "cells" if value == "cells" else "bins"})
            if rows and rows > LARGE_TABLE_ROWS:
                warnings.append(
                    f"The {table_label} table is {_count(rows)} rows. It "
                    "loads, but gating and colouring by a gene move that many "
                    "values per change; 8 µm bins are the interactive choice.")
            table.detail = describe(table)
            layers.append(table)
        else:
            layers.append(LayerProposal(
                id=f"table_{value}", kind="table", role="note",
                modality="expression", label=table_label, src=str(matrix),
                bundle=bundle,
                detail=(f"{_count(rows)} rows · recorded — import again with "
                        "this table chosen to analyse it" if rows else
                        "recorded — import again with this table chosen")))

    # -- Space Ranger 4 segmentation -------------------------------------
    if segmentation and chosen == "cells":
        # The mask ONLY with the cells table: its pixel values are cell ids,
        # and over a table of bins they would join to whichever bin happened
        # to share the number.
        mask = LayerProposal(
            id="cell_boundaries", kind="shapes", modality="mask", role="mask",
            label="Cell segmentation", src=str(segmentation["cells"]),
            bundle=bundle, transform=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
            frame="fullres", transform_source="run",
            render={"detail": "Space Ranger cell polygons"})
        mask.detail = describe(mask)
        layers.append(mask)
    elif segmentation:
        layers.append(LayerProposal(
            id="cell_boundaries", kind="shapes", role="note",
            modality="cell_boundaries", label="Cell segmentation",
            src=str(segmentation["cells"]), bundle=bundle,
            detail="Space Ranger cell polygons · used when the table is "
                   "Segmented cells"))
    if segmentation:
        if segmentation.get("nuclei"):
            layers.append(LayerProposal(
                id="nucleus_boundaries", kind="shapes", role="note",
                modality="nucleus_boundaries", label="Nucleus boundaries",
                src=str(segmentation["nuclei"]), bundle=bundle,
                detail="Nucleus polygons · recorded, not drawn yet"))
    return layers, questions, bundle, warnings


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

def _detect(path, answers, declared=None):
    """What one picked path is. `(layers, questions, bundle, warnings)`.

    @param declared - `mask`, `table` or `layer` when the user added this file
        through a card's own action rather than picking it loose. See
        `_declared`.
    """
    if is_remote_locator(path):
        return _detect_remote(str(path), answers, declared)
    path = Path(path)
    if not path.exists():
        return [], [], None, [f"{path} does not exist."]

    if path.is_dir():
        return _detect_directory(path, answers, declared)
    return _detect_file(path, answers, declared)


def _a_folder_is_not_one_file(path, layers, warnings):
    """The one thing a declared role cannot be said about.

    A run directory is read as what it HOLDS, and what it holds is several
    modalities at once -- so "this one is the mask" has nothing to name. Said
    rather than silently dropped, because the user pressed a button. Appended
    after the rest: the FIRST warning of a path that yielded nothing is the
    sentence shown to explain it, and this is not that sentence.
    """
    if not layers:
        return list(warnings)
    return list(warnings) + [
        f"{Path(path).name} is a folder, so it was read as everything inside "
        "it rather than as one file."]


def _detect_directory(path, answers, declared=None):
    from plexora.server.utils import dicom_wsi, ome_zarr

    if spatial_scene.is_visium_hd_run(path):
        # Before `is_visium_run`: one `square_008um/` folder has the scale
        # factors a standard Visium run is recognised by.
        if not _licensed(VISIUM_ENTITLEMENT):
            return _not_offered()
        layers, questions, bundle, warnings = _visium_hd_bundle(path, answers)
        if declared:
            warnings = _a_folder_is_not_one_file(path, layers, warnings)
        return layers, questions, bundle, warnings
    if spatial_scene.is_xenium_run(path):
        if not _licensed(XENIUM_ENTITLEMENT):
            return _not_offered()
        layers, questions, bundle, _ = _xenium_bundle(path)
        return layers, questions, bundle, _a_folder_is_not_one_file(
            path, layers, []) if declared else []
    if spatial_scene.is_visium_run(path):
        if not _licensed(VISIUM_ENTITLEMENT):
            return _not_offered()
        layers, questions, bundle, warnings = _visium_bundle(path)
        if declared:
            warnings = _a_folder_is_not_one_file(path, layers, warnings)
        return layers, questions, bundle, warnings
    if spatial_scene.is_spatialdata_store(path):
        layers, questions, bundle, _ = _spatialdata_bundle(path, answers)
        return layers, questions, bundle, _a_folder_is_not_one_file(
            path, layers, []) if declared else []
    if dicom_wsi.is_dicom_path(path):
        layers, questions, bundle, warnings = _dicom_slides(path, answers)
        if declared:
            warnings = _a_folder_is_not_one_file(path, layers, warnings)
        return layers, questions, bundle, warnings
    # Kept in its original place in this chain -- a SpatialData store IS a
    # `.zarr` directory, so testing for one first would take every one of them
    # down this branch. A plain zarr store is a folder on disk and one image
    # to a person, which is why a declared role means something here.
    if ome_zarr.is_zarr_image_path(path):
        return _zarr_images(path, answers, declared)

    # A plain folder. One level of recognised files rather than a recursive
    # walk: somebody who points at their home directory should get "nothing
    # here", not a five-minute scan.
    found, questions, warnings = [], [], []
    for child in sorted(path.iterdir()):
        # A 10x side file is skipped, not reported: a downloaded run is
        # `outs/` plus a handful of them, and counting any as found would
        # stop `_lone_bundle` from descending into the run they sit beside.
        if child.is_dir() or spatial_scene.is_spaceranger_side_file(child):
            continue
        layers, child_questions, _, child_warnings = _detect_file(child, answers)
        found.extend(layers)
        questions.extend(child_questions)
        warnings.extend(child_warnings)
    # A channel list is not something to import on its own, so a folder
    # holding one beside a run is still a folder that only wraps the run.
    panels = [layer for layer in found if layer.role == "channels"]
    if len(panels) == len(found):
        nested = _lone_bundle(path)
        if nested is not None:
            layers, nested_questions, bundle, nested_warnings = \
                _detect_directory(nested, answers, declared)
            return layers + panels, nested_questions, bundle, nested_warnings
        return panels, [], None, []
    masks, mask_questions = _masks_below(path, found, answers)
    found.extend(masks)
    questions.extend(mask_questions)
    if declared:
        warnings = _a_folder_is_not_one_file(path, found, warnings)
    return found, questions, None, warnings


def _masks_below(root, found, answers):
    """Segmentation masks in the folders under `root`, for the images in it
    that have none beside them. `(layers, questions)`.

    The images sit at the top of the folder and the masks a few levels down --
    `mccellpose-LSP11385/cell.ome.tif` -- so a one-level scan offered a slide
    with no mask and a "+ Add segmentation mask" to go and find the file it
    had walked straight past.

    A candidate has to have the image's exact width and height and read as
    label ids. It belongs to the image whose name its PATH carries (the folder
    usually says it, not the file); a candidate naming no image goes to an
    image only when that image is the one of its size still without a mask.
    Several for one image: whole cells first, then anything else, nuclei last
    -- and a question to change it, defaulting to that choice.
    """
    images = [layer for layer in found
              if layer.kind == "image" and layer.role == "image"
              and layer.src and not _unsettled(layer)]
    covered = {_group_stem(_named_by(layer)) for layer in found
               if layer.role == "mask"}
    lacking, seen = [], set()
    for image in images:
        stem = _group_stem(_named_by(image))
        if stem in covered or stem in seen or _size_of(image) is None:
            continue
        seen.add(stem)
        lacking.append(image)
    if not lacking:
        return [], []

    sizes = {_size_of(image) for image in lacking}
    candidates = []
    for path in _mask_files_below(Path(root)):
        geometry = _geometry_of(path)
        size = _size_of_geometry(geometry)
        if size not in sizes or int((geometry or {}).get("numChannels") or 1) != 1:
            continue
        relative = path.relative_to(root).as_posix()
        verdict = looks_like_label_image(path)
        if verdict is False or (verdict is None and not any(
                hint in relative.lower() for hint in DEEP_MASK_HINTS)):
            continue
        candidates.append({"path": path, "relative": relative,
                           "geometry": geometry, "size": size,
                           "owner": _named_owner(relative, images)})
    if not candidates:
        return [], []

    masks, questions = [], []
    for image in lacking:
        size = _size_of(image)
        pool = [c for c in candidates
                if c["size"] == size and c["owner"] is image]
        if not pool and sum(1 for other in lacking
                            if _size_of(other) == size) == 1:
            pool = [c for c in candidates
                    if c["size"] == size and c["owner"] is None]
        if not pool:
            continue
        pool.sort(key=_mask_rank)
        question_id = f"mask-for:{image.src}"
        said = answers.get(question_id)
        chosen = (None if said == "none" else
                  next((c for c in pool if str(c["path"]) == said), pool[0]))
        if chosen is not None:
            layer = LayerProposal(
                id=_layer_id(chosen["path"]), kind="labels", role="mask",
                modality="mask", label="Segmentation mask",
                src=str(chosen["path"]), geometry=chosen["geometry"])
            layer.found_for = str(image.src)
            # A file found rather than picked can be turned down from its own
            # badge -- the folder's ✕ would take the slide with it.
            layer.switch = {
                "question": question_id, "value": str(chosen["path"]),
                "options": [{"value": str(chosen["path"]),
                             "label": "Segmentation mask"},
                            {"value": "none", "label": "Not a mask"}]}
            layer.detail = describe(layer)
            masks.append(layer)
        if len(pool) > 1 or chosen is None:
            # Under the row while there is one; on the card once "None" has
            # taken the row away, so the choice can still be undone.
            questions.append(Question(
                id=f"mask-for:{image.src}", label="Which segmentation mask?",
                kind="select",
                options=tuple({"value": str(c["path"]), "label": c["relative"]}
                              for c in pool)
                + ({"value": "none", "label": "None"},),
                default=str(pool[0]["path"]),
                scope=f"layer:{layer.id}" if chosen is not None else "sample",
                about=str(image.src)))
    return masks, questions


def _size_of(layer):
    return _size_of_geometry(layer.geometry)


def _size_of_geometry(geometry):
    """`(width, height)`, or None when the file did not say."""
    width = (geometry or {}).get("width")
    height = (geometry or {}).get("height")
    return (int(width), int(height)) if width and height else None


def _mask_files_below(root):
    """Files under `root` that could hold a mask, nearest first.

    Breadth-first to `MASK_SEARCH_DEPTH` levels, within `MASK_SEARCH_LIMIT`
    entries. A store is a folder of chunks and never a mask file, so neither
    it nor anything hidden is entered. A label pyramid Plexora built beside
    its source is the same mask as that source -- registering the source
    adopts it -- so only the source is offered.
    """
    files, level, budget = [], [root], MASK_SEARCH_LIMIT
    # One pass more than the depth: the first lists `root` itself, whose own
    # files were read by the plain-folder scan already.
    for _depth in range(MASK_SEARCH_DEPTH + 1):
        below = []
        for folder in level:
            try:
                children = sorted(folder.iterdir())
            except OSError:
                continue
            for child in children:
                budget -= 1
                if budget < 0:
                    return _without_mask_copies(files)
                name = child.name.lower()
                if name.startswith(".") or name.endswith(_CONTAINER_SUFFIXES):
                    continue
                try:
                    if child.is_dir():
                        below.append(child)
                        continue
                except OSError:
                    continue
                if folder is not root and child.suffix.lower() in (
                        ".tif", ".tiff", ".png"):
                    files.append(child)
        level = below
        if not level:
            break
    return _without_mask_copies(files)


def _without_mask_copies(files):
    """`files` less any label pyramid whose source is also among them."""
    from plexora.server.utils import segmentation_pyramid

    derived = set()
    for path in files:
        for mode in (segmentation_pyramid.MODE_FILLED,
                     segmentation_pyramid.MODE_OUTLINES):
            copy = segmentation_pyramid.derived_output_path(path, None, mode=mode)
            if copy != path:
                derived.add(copy)
    return [path for path in files if path not in derived]


def _named_owner(relative, images):
    """The image whose name a mask's path carries as a whole word, or None.

    The longest such name wins, so `seg-LSP11385_rv/cell.tif` is
    LSP11385_rv's even though it also says LSP11385. Four characters at least,
    for the reason `_best_sample` gives.
    """
    text = relative.lower()
    best, best_length = None, 0
    for image in images:
        stem = _group_stem(_named_by(image))
        if len(stem) < 4 or len(stem) <= best_length:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(stem)}(?![a-z0-9])", text):
            best, best_length = image, len(stem)
    return best


def _mask_rank(candidate):
    """Which of one image's masks is proposed: a whole-cell one, then anything
    that is not nuclei, then nuclei; nearest, then by name, within each."""
    relative = candidate["relative"]
    name = relative.rsplit("/", 1)[-1].lower().split(".", 1)[0]
    words = set(re.split(r"[^a-z0-9]+", name))
    if words & {"cell", "cells", "wholecell"}:
        tier = 0
    elif "nucle" in name or "nuc" in words:
        tier = 3
    elif "cell" in name:
        tier = 1
    else:
        tier = 2
    return (tier, relative.count("/"), relative.lower())


def _not_offered():
    """A 10x run on a licence without spatial transcriptomics: nothing.

    Returned in place of the bundle, and deliberately not the plain-folder
    ladder below, which would offer the run's morphology TIFF, its cell table
    and its transcripts parquet one file at a time -- the modality piecemeal.
    No warning either: the pick reads as "Nothing Plexora can read here", the
    same as any folder this plan has no reader for.
    """
    return [], [], None, []


#: How many entries of an unreadable folder are looked at before giving up on
#: finding a bundle inside it. A folder holding a run holds a handful of
#: things; one holding thousands is somebody's downloads directory, and the
#: honest answer there is "nothing here" rather than a stat per file.
BUNDLE_SCAN_LIMIT = 200


def _is_bundle(path) -> bool:
    return (spatial_scene.is_visium_hd_run(path)
            or spatial_scene.is_xenium_run(path)
            or spatial_scene.is_visium_run(path)
            or spatial_scene.is_spatialdata_store(path))


def _lone_bundle(path):
    """The single bundle folder inside `path`, or None.

    The wrapper directory: a Xenium run arrives as a zip and unpacks to
    `Xenium_..._outs/` inside a folder of the same name, or somebody keeps the
    run under `<sample>/outs/`. Picking the folder they think of as the sample
    found nothing at all, which reads as "Plexora cannot open Xenium data" and
    is one wrong click away from being right.

    EXACTLY one, and only when the folder itself held nothing readable. Two
    runs side by side stay unexpanded on purpose: picked paths that carry
    bundles are assembled into ONE sample, so guessing on the user's behalf
    there would silently merge two slides into one -- and the user who wants
    both can pick both, which says which is which.
    """
    root = Path(path)
    if _is_bundle(root):
        return None
    found = []
    for index, child in enumerate(sorted(root.iterdir())):
        if index >= BUNDLE_SCAN_LIMIT:
            return None
        if child.is_dir() and _is_bundle(child):
            found.append(child)
            if len(found) > 1:
                return None
    return found[0] if found else None


def _dicom_slides(path, answers):
    from plexora.server.utils import dicom_wsi

    try:
        dicom_wsi.assemble_slide(path)
    except Exception as error:
        hint = _install_hint(error)
        if hint:
            layer = LayerProposal(
                id=_layer_id(path), kind="image", role="image", reference=True,
                modality="he", label=_name_of(path), src=str(path),
                dependency=hint, needs=("install",),
                detail="DICOM slide · needs an extra package")
            return [layer], [], None, []
        return [], [], None, [f"{_name_of(path)}: {error}"]
    layer = LayerProposal(
        id=_layer_id(path) or "slide", kind="image", role="image",
        reference=True, modality="he", label=_name_of(path), src=str(path))
    layer.geometry = _geometry_of(path)
    layer.channels = _channel_stubs(path, layer.geometry)
    layer.pixel_size = _pixel_size_of(path)
    layer.detail = describe(layer)
    return [layer], [], None, []


def _detect_remote(url, answers, declared=None):
    """A web address: an OME-Zarr store read through the chunk cache.

    Everything a local store is asked is asked here too, through the same
    `_zarr_images`; only the failures are different -- a scheme whose package
    is missing, a host that does not answer, an address that is not a zarr
    node -- and each says which.
    """
    from plexora.server.providers.base import RemoteUnreachable
    from plexora.server.utils import ome_zarr, remote_image, remote_store

    name = remote_store.url_name(url)
    # A file names its own kind: a DICOM instance, a TIFF, an Imaris file or
    # a picture is streamed and proposed as the image it is, with no zarr
    # probing.
    try:
        named = remote_image.kind_or_none(url, probe=False)
    except Exception:  # noqa: BLE001
        named = None
    if named == remote_image.DICOM:
        return _dicom_slides(url, answers)
    if named in (remote_image.TIFF, remote_image.IMS, remote_image.PICTURE):
        return _remote_image_layer(url, answers, declared)
    try:
        remote_image.kind_of(url, probe=False)
    except ValueError as error:
        if "local copy" in str(error):
            return [], [], None, [str(error)]
    data_type, found_layers, found_questions, warnings = _remote_tables(url, answers)
    if data_type == "anndata":
        # An AnnData holds cells and nothing to draw them on.
        return found_layers, found_questions, None, warnings
    try:
        candidates = ome_zarr.image_candidates(url)
    except remote_store.RemoteSupportMissing as error:
        return [], [], None, [f"{name}: {error}"]
    except RemoteUnreachable as error:
        return [], [], None, [
            f"Could not reach {remote_store.host_of(url)} to read {name}. "
            f"Check the address and your connection. ({error})"]
    except Exception as error:  # noqa: BLE001 -- said, not raised
        return [], [], None, [f"{name}: {error}"]
    if not candidates:
        if found_layers:
            return found_layers, found_questions, None, warnings
        result = remote_store.probe(url)
        if result.status == "offline":
            return [], [], None, [result.detail]
        if result.status == "inaccessible":
            return [], [], None, [
                f"{name}: {result.detail}. A private bucket needs its access "
                "set up in Settings > Web data."]
        if result.status == "ok" or result.status == "missing":
            # Not zarr. A folder of DICOM instances answers to a listing.
            from plexora.server.utils import dicom_wsi

            if dicom_wsi.is_dicom_path(url):
                return _dicom_slides(url, answers)
        if result.status == "missing":
            return [], [], None, [
                f"Nothing at {url} answers as a zarr store (no zarr.json, "
                ".zgroup or .zattrs there) or as a folder of DICOM instances. "
                "A DICOM folder is found only when its host lists folders; on "
                "an https:// gateway, paste the address of one .dcm file."]
    layers, questions, bundle, image_warnings = _zarr_images(
        url, answers, declared, candidates=candidates)
    return (layers + found_layers, questions + found_questions, bundle,
            image_warnings + warnings)


def _remote_image_layer(url, answers=None, declared=None):
    """A TIFF-family file or a picture at a web address, as one layer.

    `_detect_image` over the network. A file is a mask when the mask-or-image
    question was answered so, when it was added as one and its header says it
    is one plane of integers, or when `_remote_label_verdict` finds ids in it
    -- a few windows' tiles, never a whole slide. When that cannot say, the
    question is asked, as it is of a local file. Either way a single plane
    keeps the row's image/mask switch, so a wrong reading costs a click.
    """
    from plexora.datasource import _sniff_quick_view_kind
    from plexora.server.providers import local
    from plexora.server.providers.base import RemoteUnreachable
    from plexora.server.utils import remote_store, segmentation_pyramid

    name = remote_store.url_name(url)
    answer = _said(answers or {}, "mask-or-image:", url)
    question_id = f"mask-or-image:{str(url).strip()}"
    questions, warnings = [], []
    if answer:
        is_mask = answer == "mask"
    elif declared == "mask":
        is_mask = _remote_label_plane(url)
        if not is_mask:
            refusal = segmentation_pyramid.mask_format_refusal(url)
            warnings.append(
                f"{refusal} It was added as a segmentation mask, so it is "
                "proposed as an image instead." if refusal else
                f"{name} was added as a segmentation mask, but it is not one "
                "plane of integer labels -- proposed as an image instead.")
    else:
        verdict = _remote_label_verdict(url)
        if verdict is None:
            questions.append(Question(
                id=question_id,
                label=f"Is {name} a segmentation mask or an image?",
                options=({"value": "image", "label": "An image"},
                         {"value": "mask", "label": "A segmentation mask"}),
                default="image"))
        is_mask = bool(verdict)
    if is_mask:
        layer = LayerProposal(
            id=_layer_id(url), kind="labels", role="mask", modality="mask",
            label="Segmentation mask", src=str(url), geometry=_geometry_of(url))
        layer.switch = _role_switch(question_id, "mask")
        layer.detail = describe(layer)
        return [layer], questions, None, warnings
    try:
        kind = _sniff_quick_view_kind(url)
    except RemoteUnreachable as error:
        return [], [], None, [
            f"Could not reach {remote_store.host_of(url)} to read {name}. "
            f"Check the address and your connection. ({error})"]
    except Exception as error:  # noqa: BLE001 -- said, not raised
        return [], [], None, [f"{name}: {error}"]
    detection = None
    if kind != "rgb":
        try:
            detection = local.detect_image_type(url)
        except Exception:  # noqa: BLE001
            detection = None
    brightfield_like = bool(getattr(detection, "verdict", None) == "brightfield")
    rgb = kind == "rgb" or brightfield_like
    layer = LayerProposal(
        id=_layer_id(url), kind="image", role="image", reference=True,
        label=name, src=str(url),
        modality=("picture" if kind == "rgb"
                  else ("he" if brightfield_like else "multiplex")))
    if kind != "rgb":
        layer.geometry = _geometry_of(url, rgb=rgb)
        layer.channels = _channel_stubs(url, layer.geometry)
        layer.pixel_size = _pixel_size_of(url)
        if (not rgb and _single_plane(layer.geometry)
                and segmentation_pyramid.mask_format_refusal(url) is None):
            layer.switch = _role_switch(question_id, "image")
    if rgb:
        layer.render = {"rgb": True}
    layer.needs = tuple(q.id for q in questions)
    layer.detail = describe(layer)
    return [layer], questions, None, warnings


def _remote_label_plane(url) -> bool:
    """Whether the file at `url` is one plane of integers, the shape of a
    mask. Headers only: a PNG's first bytes, a TIFF's first IFD."""
    import numpy as np

    from plexora.server.utils import segmentation_pyramid

    if segmentation_pyramid.mask_format_refusal(url) is not None:
        return False
    if segmentation_pyramid.is_ims(url):
        # Added as a mask: unless no channel holds ids (`looks_like_label_image`
        # takes an undecided .ims added as a mask the same way).
        from plexora.server.volume.sources import ims

        return ims.mask_verdict(url) is not False
    if segmentation_pyramid.is_png(url):
        return segmentation_pyramid.png_header(url) is not None
    try:
        plane, close = segmentation_pyramid._open_level_zero(url)
    except Exception:  # noqa: BLE001 -- unreadable is not a mask
        return False
    try:
        return len(plane.shape) == 2 and np.dtype(plane.dtype).kind in "ui"
    finally:
        close()


#: What the label test may fetch to judge a file at a web address: every tile
#: or strip its windows touch, together. A tiled mask's windows are a few
#: tiles; a plane written as one strip would come down whole to be judged, and
#: is asked about instead (`_remote_label_verdict`).
REMOTE_LABEL_BUDGET = 16 * 1024 ** 2


def _remote_label_verdict(url):
    """`looks_like_label_image` for a file at a web address: True, False or None.

    Judged by the same windows a local file is, so a mask named like anything
    at all -- MCMICRO's `cell.ome.tif` -- is found the way it is on this
    computer. The header goes first: anything but one plane of integers is an
    image, with nothing fetched. The pixels are read only when their windows
    cost at most `REMOTE_LABEL_BUDGET`; a plane that would have to come down
    whole is left to its name, and then to the user (None).
    """
    import numpy as np

    from plexora.server.utils import remote_store, segmentation_pyramid

    if segmentation_pyramid.is_ims(url):
        # Asked of the channels themselves, as on disk; each channel's middle
        # window is fetched in one batch (volume/sources/ims.mask_verdict).
        from plexora.server.volume.sources import ims

        return ims.mask_verdict(url)
    if not _remote_label_plane(url):
        return False
    name = remote_store.url_name(url)
    try:
        if segmentation_pyramid.is_png(url):
            # No tiles: the whole file is the one window there is.
            if remote_store.open_file(url).size > REMOTE_LABEL_BUDGET:
                return True if _name_says_mask(name) else None
            plane = segmentation_pyramid.read_png_labels(url)
            return _plane_verdict(name, plane.dtype, plane.shape[-2:],
                                  lambda rows, cols: plane[rows, cols])
        plane, close = segmentation_pyramid._open_level_zero(url)
    except Exception:  # noqa: BLE001 -- unreadable says nothing either way
        return None
    try:
        height, width = (int(size) for size in plane.shape[-2:])
        windows = [(slice(top, top + LABEL_WINDOW), slice(left, left + LABEL_WINDOW))
                   for top, left in _window_origins(height, width)]
        cost = getattr(plane, "fetch_bytes", None)
        if cost is None or cost(windows) > REMOTE_LABEL_BUDGET:
            return True if _name_says_mask(name) else None
        return _plane_verdict(name, np.dtype(plane.dtype), (height, width),
                              lambda rows, cols: np.asarray(plane[rows, cols]))
    except Exception:  # noqa: BLE001
        return None
    finally:
        close()


def _remote_tables(url, answers):
    """The cell table a remote store holds: `(data_type, layers, questions,
    warnings)`, with `data_type` None when the address is not a table store.

    An AnnData-zarr is one
    table; a SpatialData store's tables are named from its consolidated
    metadata or a listing, and the question is asked the way the local
    SpatialData bundle asks it -- no default when there are several, because
    the wrong table is a different set of cells.
    """
    from plexora.server.models.adapters import detect_data_type
    from plexora.server.models.adapters.spatialdata_adapter import list_spatialdata_tables
    from plexora.server.utils import ome_zarr

    try:
        root = ome_zarr._RemoteView.of(url).ome()
    except Exception:  # noqa: BLE001 -- detection below says what went wrong
        root = {}
    if any(key in root for key in ("multiscales", "plate", "well", "bioformats2raw.layout")):
        # An image, a plate or a series store: not a table, and asking would
        # cost a round of requests for groups that are not there.
        return None, [], [], []
    try:
        data_type = detect_data_type(url)
    except ValueError:
        return None, [], [], []
    if data_type == "anndata":
        layer = LayerProposal(
            id="table", kind="table", role="table", modality="cells",
            label="Cells", src=str(url), table=None, render={"detail": "anndata"})
        layer.detail = describe(layer)
        return data_type, [layer], [], []
    try:
        tables = list_spatialdata_tables(url)
    except ValueError as error:
        return data_type, [], [], [str(error)]
    if not tables:
        return data_type, [], [], []
    names = [t["name"] for t in tables]
    chosen = answers.get("table")
    table_name = chosen if chosen in names else (names[0] if len(names) == 1 else None)
    layer = LayerProposal(
        id="table", kind="table", role="table", modality="cells", label="Cells",
        src=str(url), table=table_name,
        render={"detail": f"table {table_name}" if table_name
                else f"{len(names)} tables — which holds the cells?"})
    questions = []
    if len(names) > 1:
        questions.append(Question(
            id="table", scope="layer:table", label="Which table holds the cells?",
            kind="select", options=tuple({"value": n, "label": n} for n in names),
            default=table_name))
        if not table_name:
            layer.needs = ("table",)
    layer.detail = describe(layer)
    return data_type, [layer], questions, []


def _zarr_images(path, answers, declared=None, candidates=None):
    if candidates is None:
        candidates = _zarr_image_candidates(path)
    if not candidates:
        name = path.rstrip("/").rsplit("/", 1)[-1] if is_remote_locator(path) \
            else Path(path).name
        return [], [], None, [
            f"{name} is a zarr store with no OME-Zarr image in it."]
    chosen = answers.get("image")
    picked = next((c for c in candidates if c[0] == chosen), candidates[0])
    questions = []
    if len(candidates) > 1:
        questions.append(Question(
            id="image", label="Which image?", kind="select",
            options=tuple({"value": name, "label": name}
                          for name, _ in candidates),
            default=picked[0]))
    remote = is_remote_locator(picked[1])
    question_id = f"mask-or-image:{str(picked[1]).strip()}"
    answer = _said(answers, "mask-or-image:", picked[1])
    warnings = []
    if answer:
        verdict = answer == "mask"
    else:
        # A store usually says which it is -- `image-label` in a label
        # image's attributes, `omero` channels in an image's -- and one that
        # does not is read like a TIFF plane. A web store is taken as an
        # image unless the action it arrived through said otherwise: reading
        # pixels over the network to answer this is not worth the wait.
        verdict = None if remote else _zarr_label_verdict(picked[1])
        if declared == "mask":
            if verdict is False:
                warnings.append(
                    f"{picked[0]} was added as a segmentation mask, but its "
                    "pixels are an image -- proposed as an image instead.")
            else:
                # The reading the store was consistent with anyway, settled by
                # the person who has the data in front of them.
                verdict = True
    geometry = _geometry_of(picked[1])
    if verdict:
        layer = LayerProposal(
            id=picked[0], kind="labels", role="mask", modality="mask",
            label="Segmentation mask", src=str(picked[1]),
            geometry=geometry)
        layer.switch = _role_switch(question_id, "mask")
        layer.detail = describe(layer)
        return [layer], questions, None, warnings
    layer = LayerProposal(
        id=picked[0], kind="image", role="image", reference=True,
        modality="multiplex", label=picked[0], src=str(picked[1]))
    layer.geometry = geometry
    layer.channels = _channel_stubs(picked[1], layer.geometry)
    layer.pixel_size = _pixel_size_of(picked[1])
    if _single_plane(geometry):
        layer.switch = _role_switch(question_id, "image")
    layer.detail = describe(layer)
    layers = [layer]
    if remote:
        layers += _remote_label_masks(picked[1], answers, questions)
    else:
        layers += _local_label_masks(Path(picked[1]), answers, questions)
    return layers, questions, None, warnings


def _local_label_masks(image, answers, questions):
    """The image's own `labels/` elements, as its segmentation -- the local
    twin of `_remote_label_masks`.

    An NGFF writer files a label image INSIDE the image it segments
    (`image.zarr/labels/cells`), so the folder somebody picked already holds
    the cells. Read off the store's `labels` attribute, or a listing when it
    has none; one becomes the mask, asked when there are several, with "None"
    to leave them out.
    """
    from plexora.server.utils import ome_zarr

    holder = image / "labels"
    try:
        if not holder.is_dir():
            return []
        listed = ome_zarr._ome_attrs(ome_zarr._open_group(holder)).get("labels")
    except Exception:  # noqa: BLE001 -- no masks is an answer, not a failure
        return []
    if isinstance(listed, (list, tuple)):
        names = [str(name).strip("/") for name in listed if str(name).strip("/")]
    else:
        names = ome_zarr._zarr_children(holder)
    names = [name for name in names if (holder / name).is_dir()]
    if not names:
        return []
    chosen = answers.get("labels")
    picked = None if chosen == "none" else (chosen if chosen in names else names[0])
    if len(names) > 1 or chosen == "none":
        questions.append(Question(
            id="labels", label="Which segmentation?", kind="select",
            options=tuple({"value": n, "label": n} for n in names)
            + ({"value": "none", "label": "None"},),
            default=picked or "none"))
    if picked is None:
        return []
    mask = LayerProposal(
        id=_clean_name(f"labels_{picked}") or "labels", kind="labels",
        role="mask", modality="mask", label=f"Segmentation · {picked}",
        src=str(holder / picked), geometry=_geometry_of(holder / picked))
    mask.switch = {"question": "labels", "value": picked,
                   "options": [{"value": picked, "label": "Segmentation mask"},
                               {"value": "none", "label": "Not a mask"}]}
    mask.detail = describe(mask)
    return [mask]


def _remote_label_masks(url, answers, questions):
    """The image's `labels/` elements, as its segmentation.

    An NGFF image names its label images in `labels/.zattrs`, which is how IDR
    ships segmentations: the store that holds the picture also holds the cells.
    One of them becomes the mask -- asked when there are several -- and it is
    converted into the project once, through the chunk cache.
    """
    from plexora.server.utils import ome_zarr, remote_store

    try:
        names = ome_zarr.label_names(ome_zarr._RemoteView.of(url))
    except Exception:  # noqa: BLE001 -- no masks is an answer, not a failure
        return []
    if not names:
        return []
    chosen = answers.get("labels")
    if chosen == "none":
        picked = None
    else:
        picked = chosen if chosen in names else names[0]
    if len(names) > 1 or chosen == "none":
        questions.append(Question(
            id="labels", label="Which segmentation?", kind="select",
            options=tuple({"value": n, "label": n} for n in names)
            + ({"value": "none", "label": "None"},),
            default=picked or "none"))
    if picked is None:
        return []
    src = remote_store.url_join(url, "labels", picked)
    mask = LayerProposal(
        id=_clean_name(f"labels_{picked}") or "labels", kind="labels", role="mask",
        modality="mask", label=f"Segmentation · {picked}", src=src,
        geometry=_geometry_of(src))
    mask.detail = describe(mask)
    return [mask]


def _detect_file(path, answers, declared=None):
    """One file, by content. `(layers, questions, bundle, warnings)`."""
    suffix = path.suffix.lower()

    if spatial_scene.is_spaceranger_side_file(path):
        return [], [], None, [
            f"{path.name} is a Space Ranger side file, not a layer or a "
            "table. Pick the run's outs folder (or the folder holding it) "
            "to import the run."]

    if suffix == ".parquet":
        return _detect_parquet(path)
    if suffix in (".geojson", ".json") and _is_feature_collection(path):
        layer = LayerProposal(
            id=_layer_id(path), kind="shapes", modality="annotations",
            label=path.stem.replace("_", " ").title(), src=str(path),
            detail="Shapes · registered; drawing them is not built yet")
        return [layer], [], None, []
    if suffix in IMAGE_SUFFIXES:
        return _detect_image(path, answers, declared)
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
    """A parquet, by its columns: transcripts, a cell table, or neither.

    Read from the footer, so a 6 GB transcript table is classified in
    milliseconds. The cell-table branch is what a Xenium run's `cells.parquet`
    lands in and what `adapters` reads back as `type="parquet"` -- the same
    flat table a CSV is, in the encoding every vendor now writes.
    """
    from plexora.server.models.adapters import classify

    if spatial_scene.is_xenium_transcripts(path):
        if not _licensed(XENIUM_ENTITLEMENT):
            # Not offered, and not allowed on to the cell-table test below,
            # whose x/y guess would read every molecule as a cell.
            return _not_offered()
        peek = spatial_scene.peek_parquet(path)
        layer = LayerProposal(
            id="transcripts", kind="points", modality="transcripts",
            label="Transcripts", src=str(path),
            geometry=_points_extent(path),
            render={"detail": f"{_count(peek['rows'])} molecules"} if peek else {})
        layer.detail = describe(layer)
        return [layer], [], None, []

    # Before the cell-table test, and that order is the point: a boundary
    # table's `vertex_x`/`vertex_y` are exactly the kind of names
    # `guess_roles` reads as centroids, so a segmentation picked on its own
    # would register as a cell table with one row per polygon vertex.
    if boundary_mask.is_boundary_table(path):
        peek = spatial_scene.peek_parquet(path)
        layer = LayerProposal(
            id=_layer_id(path), kind="shapes", role="mask", modality="mask",
            label="Cell segmentation", src=str(path),
            render={"detail": f"{_count(peek['rows'])} boundary vertices"}
            if peek else {})
        layer.detail = describe(layer)
        return [layer], [], None, []

    peek = spatial_scene.peek_parquet(path)
    if peek is None:
        return [], [], None, [
            f"{path.name}: reading parquet needs pyarrow "
            '(pip install "plexora[spatial]").']

    names = list(peek["columns"])
    # Xenium's own shape first, then anything a centroid can be read out of.
    # The second test is what makes a cell table exported by some other
    # pipeline work: the file is flat and the adapter reads it either way, and
    # a table whose coordinates cannot be found is one nothing could place
    # cells from -- so that, and not the vendor, is the line.
    cells = spatial_scene.looks_like_cells_parquet(path)
    if not cells:
        roles = classify.guess_roles(names)
        cells = bool(roles.get("x") and roles.get("y"))
    if cells:
        layer = LayerProposal(
            id="cells", kind="table", role="table", modality="cells",
            label="Cells", src=str(path),
            render={"detail": f"{_count(peek['rows'])} rows × "
                              f"{len(names)} columns · parquet"})
        layer.detail = describe(layer)
        return [layer], [], None, []

    return [], [], None, [
        f"{path.name} is a parquet Plexora does not recognise "
        f"(columns: {', '.join(names[:6])})."]


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


def _detect_image(path, answers, declared=None):
    from plexora.datasource import _sniff_quick_view_kind
    from plexora.server.providers import local

    verdict = looks_like_label_image(path)
    # The whole path, not the name: see `_said`. Two samples' `cell.tif` are
    # two questions.
    answer = _said(answers, "mask-or-image:", path)
    questions, warnings = [], []
    if answer:
        # An answer to the question wins over the action the file arrived
        # through: the question is asked later and about this file by name.
        verdict = answer == "mask"
    elif declared == "mask":
        if verdict is None:
            # The reading the pixels were consistent with anyway, settled by
            # the person who has the run in front of them. No question: it has
            # already been answered.
            verdict = True
        elif verdict is False:
            from plexora.server.utils import segmentation_pyramid

            refusal = segmentation_pyramid.mask_format_refusal(path)
            warnings.append(
                f"{refusal} It was added as a segmentation mask, so it is "
                "proposed as an image instead." if refusal else
                f"{path.name} was added as a segmentation mask, but its "
                "pixels are an image -- proposed as an image instead.")
    elif declared == "table" and verdict is False:
        warnings.append(
            f"{path.name} was added as data, but it is an image -- proposed "
            "as an image instead.")

    if verdict is None:
        questions.append(Question(
            id=f"mask-or-image:{str(path).strip()}",
            label=f"Is {path.name} a segmentation mask or an image?",
            options=({"value": "image", "label": "An image"},
                     {"value": "mask", "label": "A segmentation mask"}),
            default="image"))
        verdict = False

    question_id = f"mask-or-image:{str(path).strip()}"
    if verdict:
        layer = LayerProposal(
            id=_layer_id(path), kind="labels", role="mask", modality="mask",
            label="Segmentation mask", src=str(path),
            geometry=_geometry_of(path))
        layer.needs = tuple(q.id for q in questions)
        layer.switch = _role_switch(question_id, "mask")
        layer.detail = describe(layer)
        return [layer], questions, None, warnings

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
        return [layer], questions, None, warnings

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
    if not rgb and _single_plane(layer.geometry):
        # One plane of a format that can hold ids: it could be a mask, and
        # the role menu says so whatever detection concluded.
        from plexora.server.utils import segmentation_pyramid

        if segmentation_pyramid.mask_format_refusal(path) is None:
            layer.switch = _role_switch(question_id, "image")
    if rgb:
        layer.render = {"rgb": True}
    else:
        # The same sentence a Xenium run's own morphology row gets, for a
        # focus stack somebody picked as a loose file. "1 channel" over a file
        # that is visibly fourteen planes reads as a failure to open it;
        # saying which plane was kept turns it into a decision.
        note = _focal_note(path)
        if note:
            layer.render = {"detail": note}
    layer.needs = tuple(q.id for q in questions)
    if not rgb and kind != "ome_zarr":
        # Not in `needs`: it is a confirmation the dialog puts to the user as
        # a modal at Import, and an unanswered one means "build it", which is
        # what the API does too. Nothing is recorded as outstanding.
        offer = _pyramid_offer(path, layer)
        if offer is not None:
            questions.append(offer)
    layer.detail = describe(layer)
    return [layer], questions, None, warnings


def _pyramid_offer(path, layer):
    """The pyramidize question for a large flat multiplex TIFF, or None.

    Header-only (`image_pyramid.flat_image_report`). Fills `layer.render`
    with what the modal states -- size, channels, levels, the estimated size
    of the copy and where it would go -- so the dialog never has to ask.
    """
    from plexora.server.utils import image_pyramid

    geometry = layer.geometry or {}
    if geometry.get("maxLevel") not in (None, 1):
        return None
    if image_pyramid.is_image_copy(path):
        return None
    report = image_pyramid.flat_image_report(path)
    if report is None:
        return None
    where = image_pyramid.resolve_derived_image(path)
    if where.existing is not None:
        # A finished copy of this exact file is already there and is what
        # registration will open. Nothing to ask.
        note = (layer.render or {}).get("detail")
        layer.render = {**(layer.render or {}),
                        "detail": f"{note} · Pyramidized copy found" if note
                        else "Pyramidized copy found"}
        return None
    target = where.target
    render = dict(layer.render or {})
    render["pyramid"] = {**report, "output": str(target), "output_name": target.name,
                         "location": ("beside the original (or in the project's "
                                      "folder if that one is not writable)")}
    note = render.get("detail")
    render["detail"] = f"{note} · No pyramid" if note else "No pyramid"
    layer.render = render
    return Question(
        id=f"pyramidize:{str(path).strip()}",
        label="Build a pyramidized copy before opening?",
        options=({"value": "yes", "label": "Pyramidize and import"},
                 {"value": "no", "label": "Import as is"}),
        default="yes", scope=f"layer:{layer.id}", kind="confirm")


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


def _marker_sheet(path):
    """`(grid, column, has_header)` for a channel list, or None for any other
    file.

    Decided from the header line alone, so a 200 MB cell table costs one
    readline here; only a file that IS a channel list is read whole, through
    `channel_file` -- the parser the viewer's channel-names upload uses, so a
    sheet that works there reads the same here. `has_header` is None for a
    one-column `markers.csv`, whose first row may be a title or a marker:
    the image's channel count decides (`_panel_names`).
    """
    from plexora.server.utils import channel_file

    path = Path(path)
    if path.suffix.lower() not in (".csv", ".tsv", ".txt"):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as handle:
            header = handle.readline().strip()
    except OSError:
        return None
    delimiter = next((d for d in (",", "\t", ";") if d in header), None)
    columns = [cell.strip().strip('"').strip().lower()
               for cell in (header.split(delimiter) if delimiter else [header])]
    if MARKER_SHEET_COLUMN in columns:
        column, has_header = columns.index(MARKER_SHEET_COLUMN), True
    elif path.name.lower() in MARKER_SHEET_FILES and len(columns) == 1:
        column, has_header = 0, None
    else:
        return None
    try:
        grid = channel_file.read_grid(path=str(path))
    except channel_file.ChannelFileError:
        return None
    return grid, column, has_header


def _panel_names(path, count):
    """The `count` channel names a channel list holds, or None when it does
    not hold exactly that many: a list of the wrong length renames every
    channel after the first missing one to its neighbour's marker, and
    nothing on screen would say so."""
    from plexora.server.utils import channel_file

    sheet = _marker_sheet(path)
    if sheet is None or count <= 0:
        return None
    grid, column, has_header = sheet
    if has_header is None:
        has_header = channel_file.autodetect(grid, count)
        if has_header is None:
            return None
    names = channel_file.names(grid, column, has_header)
    return names if len(names) == count else None


def _detect_table(path):
    from plexora.server.models.adapters import detect_data_type

    if _marker_sheet(path) is not None:
        # Not a table: the names of the image's channels. Carried as a row so
        # it travels with its pick, and taken out again before grouping
        # (`inspect_paths`) -- it belongs to every sample its images make,
        # and asking which one would be asking a question with one answer.
        # No modality: it never reaches the screen as a row of its own.
        layer = LayerProposal(id="channel_names", kind="channels",
                              role="channels", label=path.name, src=str(path))
        return [layer], [], None, []
    if path.suffix.lower() in (".csv", ".tsv", ".txt") and not _looks_like_a_table(path):
        return [], [], None, [
            f"{path.name} has no delimited header — it does not look like "
            "a table."]
    try:
        data_type = detect_data_type(path)
    except ValueError as error:
        return [], [], None, [str(error)]
    size = _table_size(path)
    detail = data_type
    if size is not None:
        rows, columns, estimated = size
        detail = (f"{'~' if estimated else ''}{_count(rows)} rows × "
                  f"{columns} columns · {data_type}")
    layer = LayerProposal(
        id="table", kind="table", role="table", modality="cells",
        label="Cells", src=str(path), table=None,
        render={"detail": detail})
    if data_type == "anndata":
        layer.subset = _image_column(path)
    layer.detail = describe(layer)
    return [layer], [], None, []


def _image_column(path):
    """`{column, values}` when this AnnData spans several images, else None.

    The same reading the requirements modal's subset picker is built from
    (`propose_read_spec`'s `ambiguous`), and the same cached inspection --
    once per file version, about a second on a 5M-cell table.
    """
    from plexora.server.models.adapters import inspection

    try:
        document = inspection.inspect_anndata(path)
        ambiguous = inspection.propose_read_spec(document).get("ambiguous") or []
    except Exception:  # noqa: BLE001 -- an unreadable table asks later, as before
        return None
    for column in ambiguous:
        values = next((list(entry.get("values") or []) for entry in
                       document.get("obs_columns") or []
                       if entry.get("name") == column), [])
        if len(values) > 1:
            return {"column": column, "values": values}
    return None


def _id_tokens(text):
    """A name as the words and numbers it is made of: `LSP10003_tumor` ->
    ("lsp", 10003, "tumor"). Numbers compare as numbers, so `LSP0003` is
    `LSP3` -- and never as text, so `LSP1` is not a prefix of `LSP10003`."""
    return tuple(int(part) if part.isdigit() else part
                 for part in re.findall(r"[a-z]+|[0-9]+", str(text).lower()))


def _contains(longer, shorter) -> bool:
    size = len(shorter)
    return any(longer[at:at + size] == shorter
               for at in range(len(longer) - size + 1))


#: How sure a name has to be before it is used as the answer: a whole run of
#: one name's words found in the other's, not merely a word in common.
SUBSET_MATCH_FLOOR = 0.5


class _ExactName(str):
    """A name that says which image this is only when a value IS it: a
    folder's, which names whatever else is in it too (`2024_run3/` holds
    image 1 as much as image 3)."""


def _run_score(shorter, longer) -> float:
    """How well `shorter` names `longer` by being a run of its words; 0 when
    it is not one, or says too little to be told from chance.

    A bare number is the image's only when it is the only number in the other
    name: `slide_3` is image 3, but `run3_2024` and `scan_2024_10_14` are
    dates and run numbers that happen to contain one.
    """
    if not _contains(longer, shorter):
        return 0.0
    if all(isinstance(token, int) for token in shorter) and [
            token for token in longer if isinstance(token, int)] != list(shorter):
        return 0.0
    return 0.5 + 0.4 * len(shorter) / len(longer)


def subset_match(values, names):
    """Which of an image-id column's `values` names this image, or None.

    `names` are what the image is called -- its file name, the name its own
    metadata gives it, the sample's. A value matches a name exactly, or as a
    contiguous run of its words (`LSP10003` in `LSP10003_tumor_region.ome.tif`,
    or the other way round), scored by how much of the longer one it covers;
    a folder's name (`_ExactName`) only exactly. Only a value that wins
    outright is returned: two equally good candidates is a question for the
    user, not a coin toss. The answer is APPLIED (`_choose_subsets`), so a
    match that could be chance is no match.
    """
    scored = []
    wanted = [(tokens, isinstance(name, _ExactName))
              for tokens, name in ((_id_tokens(name), name) for name in names)
              if tokens]
    for value in values:
        tokens = _id_tokens(value)
        if not tokens:
            continue
        best = 0.0
        for name, exact in wanted:
            if tokens == name:
                score = 1.0
            elif exact:
                score = 0.0
            else:
                score = _run_score(*sorted((tokens, name), key=len))
            best = max(best, score)
        if best:
            scored.append((best, str(value)))
    if not scored:
        return None
    scored.sort(reverse=True)
    top = scored[0]
    if top[0] < SUBSET_MATCH_FLOOR:
        return None
    if len(scored) > 1 and scored[1][0] >= top[0] - 1e-9:
        return None
    return top[1]


def _ome_image_name(path):
    """The name an OME-TIFF gives its image in its own metadata, or None.
    A header read, never pixels; anything unreadable says nothing."""
    try:
        import tifffile

        with tifffile.TiffFile(str(path)) as handle:
            xml = handle.ome_metadata or ""
    except Exception:  # noqa: BLE001
        return None
    found = re.search(r'<Image\b[^>]*\bName="([^"]+)"', xml)
    if not found or _GENERIC_IMAGE_NAME.match(found.group(1)):
        return None
    return found.group(1)


#: What the software that wrote an image calls it, rather than anybody about
#: it -- "Image0", "Series 1", "Pos:2". The number in one is the image's place
#: in its file, so it names nothing a table would.
_GENERIC_IMAGE_NAME = re.compile(r"^\s*(image|series|scene|pos(ition)?)\W*\d*\s*$",
                                 re.IGNORECASE)


def _image_names(image_src, *extra):
    """What an image is called, every way it says so: its file's stem, its
    folder (a run's `LSP11385/` names it there -- only ever exactly), the
    name in its own metadata, and whatever the caller adds (the sample's)."""
    names = [name for name in extra if name]
    if image_src and not is_remote_locator(image_src):
        names.append(_file_stem(image_src))
        names.append(_ExactName(Path(str(image_src)).parent.name))
        if str(image_src).lower().endswith((".tif", ".tiff")):
            names.append(_ome_image_name(image_src))
    elif image_src:
        names.append(_file_stem(_name_of(image_src)))
    return [name for name in names if name]


def _file_stem(path) -> str:
    """A file's name without its extension -- both of `.ome.tif`'s parts."""
    name = Path(str(path)).name
    lowered = name.lower()
    for suffix in (".ome.tiff", ".ome.tif", ".ome.zarr", ".ome.btf"):
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def _choose_subsets(samples, answers, scoped_image=None):
    """For each sample's multi-image table, the image it reads -- answered,
    else matched by name -- and a question to change it.

    The default is APPLIED: the table is registered reading that image's cells,
    so the sample opens with its cells rather than as an image waiting for
    somebody to pick from twenty identifiers. The question stays on the card
    with the match chosen (and a ✓ to settle it), because a name is evidence
    rather than proof. With no match and no answer it defaults to nothing and
    the table is recorded unresolved, exactly as before.
    """
    for sample in samples:
        reference = next((layer for layer in sample.layers if layer.reference), None)
        names = (_image_names(scoped_image, sample.name) if scoped_image is not None
                 else _image_names(reference.src if reference else None, sample.name))
        for layer in sample.layers:
            if layer.role != "table" or not layer.subset:
                continue
            values = list(layer.subset.get("values") or [])
            question = Question(
                id=f"subset:{sample.key or sample.name}",
                label=f"Which image in {Path(_named_by(layer)).name} is this sample?",
                kind="select",
                options=tuple({"value": str(v), "label": str(v)} for v in values),
                scope=f"layer:{layer.id}")
            question_id = question.id
            said = answers.get(question_id)
            chosen = (said if said in values else
                      layer.subset.get("value") or subset_match(values, names))
            layer.subset = {**layer.subset, "value": chosen}
            question.default = chosen
            sample.questions.append(question)
            if chosen is None:
                layer.needs = tuple(layer.needs) + (question_id,)
            else:
                layer.needs = tuple(n for n in layer.needs if n != question_id)
                base = (layer.render or {}).get("detail") or layer.detail
                which = f"{chosen} (1 of {len(values)} images)"
                layer.detail = f"{which} · {base}" if base else which


#: Over this, a delimited table's rows are ESTIMATED from its first megabyte
#: rather than counted. Counting reads the file -- a tenth of a second for a
#: 200 MB mcmicro table, which is worth it to say the number exactly, and not
#: worth it for a 20 GB one on every inspection.
TABLE_COUNT_LIMIT = 1 << 30

#: `(path, size, mtime) -> (rows, columns, estimated) | None`. Inspection
#: re-runs on every answer, and a table that has not changed has not changed
#: size: counting it once per dialog is the whole cost.
_TABLE_SIZES: dict = {}


def _table_size(path):
    """`(rows, columns, estimated)` for a feature table, or None.

    Never a parse: a delimited file's columns are its header line and its rows
    are its line breaks; an `.h5ad` states its matrix's shape in metadata.
    """
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key in _TABLE_SIZES:
        return _TABLE_SIZES[key]
    try:
        size = (_h5ad_size(path) if path.suffix.lower() == ".h5ad"
                else _delimited_size(path, stat.st_size))
    except Exception:
        size = None
    if len(_TABLE_SIZES) >= 256:
        _TABLE_SIZES.clear()
    _TABLE_SIZES[key] = size
    return size


def _delimited_size(path, total):
    import csv

    with open(path, "rb") as handle:
        head = handle.read(1 << 20)
        line = head.split(b"\n", 1)[0].decode("utf-8-sig", errors="ignore")
        delimiter = "\t" if path.suffix.lower() == ".tsv" else max(
            (",", "\t", ";"), key=line.count)
        columns = len(next(csv.reader([line], delimiter=delimiter), []))
        if not columns or not head:
            return None
        if total > TABLE_COUNT_LIMIT:
            breaks = head.count(b"\n")
            if breaks < 2:
                return None
            return max(round(total * breaks / len(head)) - 1, 0), columns, True
        breaks, last = head.count(b"\n"), head[-1:]
        while True:
            block = handle.read(1 << 22)
            if not block:
                break
            breaks += block.count(b"\n")
            last = block[-1:]
    # The header is a line and not a row; a last row with no line break after
    # it is a row all the same.
    rows = breaks - 1 + (0 if last == b"\n" else 1)
    return max(rows, 0), columns, False


def _h5ad_size(path):
    import h5py

    from plexora.server.models.adapters.anndata_adapter import (_child,
                                                                _matrix_shape)

    with h5py.File(path, "r") as handle:
        matrix = _child(handle, "X")
        rows, columns = _matrix_shape(matrix) if matrix is not None else (None, None)
    return (rows, columns, False) if rows is not None else None


# -- grouping --------------------------------------------------------------

def _layer_id(path) -> str:
    """A layer id from a filename, without the format's own extensions.

    `slide.ome.tif` is the slide called "slide", not one called "slide.ome" --
    and the id is what appears in tile urls, in `ctx.layers.find` and on the
    card, so it is worth being the name a person would use.
    """
    return _clean_name(_name_of(path).split(".", 1)[0]) or "layer"


def _name_of(path) -> str:
    """A file's name, for a path or a web address (`Path` would fold
    `https://` and keep a presigned URL's query in the name)."""
    from plexora.server.providers.base import is_remote_locator

    if is_remote_locator(path):
        from plexora.server.utils import remote_store

        return remote_store.url_name(path)
    return Path(path).name


def _named_by(layer) -> str:
    """What a layer is grouped and named by: its path, or just its filename.

    Its own `src`, unless that is a node address -- which carries a derived
    resource id where the filename would be. The file's own name is on the
    layer in that case, and every reader here takes `Path(...).name` off the
    front of whatever it is given, so a bare name goes through the same code.
    """
    return str(layer.filename or layer.src or "")


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
        return _clean_name(bundles[0].get("name")
                           or Path(bundles[0]["root"]).name)
    if len(paths) == 1:
        inside = _name_inside_a_store(paths[0])
        if inside:
            return _clean_name(inside)
    stems = {_group_stem(p) for p in paths}
    if len(stems) == 1 and stems != {""}:
        return _clean_name(next(iter(stems)))
    return _clean_name(_file_stem(paths[0]) if paths else "sample")


def _name_inside_a_store(path):
    """A name for a path that points INSIDE a store, or None.

    `screen.zarr/B/2/0` is well B2's first field, and its bare stem is "0" --
    which is the name every other field of every other well also has, so
    importing two of them collides and the second is refused. `suggest_name`
    puts the store and the well back in front, which is what makes them
    tellable apart in a library listing.

    None for everything else, so an ordinary file keeps the name it already
    got: this is a fix for one shape of path, not a new naming scheme.
    """
    from plexora.server.utils import ome_zarr

    if not ome_zarr.is_zarr_image_path(path):
        return None
    try:
        return ome_zarr.suggest_name(path) or None
    except Exception:
        return None


#: Directory extensions that are a FORMAT rather than part of the name. A
#: SpatialData store called `sample.zarr` is the sample called "sample", and a
#: project named `sample.zarr` puts the extension in its URL and on its card.
_CONTAINER_SUFFIXES = (".zarr", ".ome.zarr", ".n5")


def _clean_name(value) -> str:
    value = str(value or "").strip()
    for suffix in sorted(_CONTAINER_SUFFIXES, key=len, reverse=True):
        if value.lower().endswith(suffix):
            value = value[: -len(suffix)]
            break
    value = re.sub(r"[^\w\-. ]+", "_", value)
    return value or "sample"


def _align(layer, reference, reference_pixel_size, run_pixel_size,
           frame_scale=None):
    """Put one layer into the reference layer's pixel grid.

    In order: what the store declared, what the run's calibration implies, the
    ratio of two stated pixel sizes, and -- when nothing says -- nothing, which
    the Layer Manager already reports as "aligned by assumption". Deliberately
    NOT a guess dressed as a measurement: a layer placed by an invented scale
    looks registered and is wrong everywhere, which is the one failure
    registration exists to prevent.
    """
    if layer.frame == "fullres":
        # Stated in a Visium run's full-res microscope frame. The reference
        # is that frame scaled by `frame_scale` (the hires PNG) or the frame
        # itself (a full-res H&E): compose, once.
        scale = float(frame_scale or 1.0)
        if layer.transform is not None and scale != 1.0:
            layer.transform = ngff_transform.compose(
                (scale, 0.0, 0.0, scale, 0.0, 0.0), layer.transform)
        layer.frame = None
        return layer
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
    # A node address is not a path and `_find_existing_datasource_for_image`
    # resolves one. Two projects reading the same resource from the same node
    # is a real duplicate and worth catching, but the node owns that identity
    # and asking it is a round trip on every keystroke of the import screen.
    if reference is not None and reference.src and not _is_node_address(reference.src):
        found = _find_existing_datasource_for_image(reference.src, config)
        if found:
            return found
    # A node-backed project records no path -- by design, because the file is
    # on another machine and that machine's layout is not this one's business.
    # What it records is the binding, so that is what the comparison is.
    if reference is not None and _is_node_address(reference.src):
        from plexora.server.routes.import_routes import _node_locator

        located = _node_locator(reference.src)
        for name, entry in config.items():
            binding = Project.from_entry(name, entry).resource("image")
            if (binding is not None and located
                    and binding.node == located[0]
                    and str(binding.resource_id) == str(located[1])):
                return name

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


# -- what the screen said about a pick -------------------------------------

def _pick_name(raw) -> str:
    """The filename at the end of a pick, local or `node://`.

    Both halves of a node address are just text here -- the far side's
    separators are its own -- so the slashes are normalised before the last
    segment is taken, rather than handing a Windows path to `PurePosixPath`
    and getting the whole thing back.
    """
    text = str(raw).strip().replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[-1]


def _said(answers, prefix, raw):
    """The answer filed under `prefix` for ONE pick, or None.

    Keyed by the whole pick, exactly as the caller sent it in `paths`, and
    that is the fix for masks landing on the wrong sample. It used to be the
    filename alone -- and an mcmicro run names every sample's mask
    `cellRing.ome.tif`, so adding sample 2's mask rewrote the answer that
    said which card sample 1's belonged to, and both landed on one. Two picks
    are two answers whatever their files are called.

    The bare filename is still read when no whole-pick answer exists, so a
    script that says `answers={"added-as:mask.tif": "mask"}` for a single
    sample keeps working. The screen never sends one.
    """
    whole = f"{prefix}{str(raw).strip()}"
    if whole in answers:
        return answers.get(whole)
    # The screen's own spelling of the same pick -- forward slashes, `~`,
    # quotes -- which the route tidied on the way in. Compared tidied.
    from plexora.server.routes.import_routes import tidy_pick

    mine = tidy_pick(raw)
    for key, value in answers.items():
        if (isinstance(key, str) and key.startswith(prefix)
                and len(key) > len(prefix)
                and _looks_like_a_path(key[len(prefix):])
                and tidy_pick(key[len(prefix):]) == mine):
            return value
    return answers.get(prefix + _pick_name(raw))


#: Questions detection asks about what is INSIDE one pick -- which image a
#: store is drawn in, which of its tables holds the cells, which bin size.
#: Their ids are plain words, so two samples' stores asked the same thing
#: shared one answer: choosing sample 1's table chose sample 2's too. Each is
#: asked as `<id>@<pick>` and answered per pick; a bare `<id>` in `answers`
#: is still read, for every pick, so a script passing `{"table": "cells"}`
#: keeps working.
PER_PICK_QUESTIONS = ("reference", "table", "image", "bin-size", "labels")


def _answers_for(answers, raw) -> dict:
    """`answers` as one pick's detection should read them: that pick's own
    answer to each per-pick question, where it gave one, under the plain id."""
    mine = dict(answers)
    for base in PER_PICK_QUESTIONS:
        value = _said(answers, f"{base}@", raw)
        if value is not None:
            mine[base] = value
    return mine


def _per_pick(question_id, raw) -> str:
    return (f"{question_id}@{str(raw).strip()}"
            if question_id in PER_PICK_QUESTIONS else question_id)


def plain_question_id(question_id) -> str:
    """The id a question is recorded under: `table`, not `table@<pick>`.

    `LayerSpec.unresolved` is read by whatever asks later, and it asks about
    the table, not about which pick the import dialog saw it in.
    """
    base, sep, _ = str(question_id).partition("@")
    return base if sep and base in PER_PICK_QUESTIONS else str(question_id)


def _declared(raw, answers) -> str | None:
    """What the user SAID a pick was, by which action they added it with.

    `+ Add segmentation mask` on a sample's card is a statement about a file,
    and it is the only kind of statement this module accepts: it settles a
    question detection would otherwise have to ask, and it never overrides
    what the pixels plainly say. A file added as a mask that turns out to be a
    three-channel image is proposed as an image, with a warning -- because the
    alternative is a cell-id lookup over a photograph, which is the failure
    the `mask-or-image` question exists to prevent.

    Carried in `answers` rather than in a second array beside `paths`, so it
    survives re-inspection, reaches the Python API and the CLI unchanged, and
    has nothing to stay aligned with.
    """
    value = str(_said(answers, "added-as:", raw) or "").strip().lower()
    return value if value in ("mask", "table", "layer") else None


def _attached(raw, answers) -> str | None:
    """The `SampleProposal.key` a pick was added to, or None if it was loose."""
    return str(_said(answers, "sample-for:", raw) or "").strip() or None


# -- the entry point -------------------------------------------------------

def inspect_paths(paths, *, node=None, answers=None, sample=None) -> Proposal:
    """What these paths hold, and what sample they would make.

    @param answers - question id -> the user's answer, applied on this pass.
        Re-inspecting with answers is how the screen narrows: a store's table
        chosen, a mask-or-image settled, a grouping decided.
    @param sample - an existing project's name, for "+ Add Layer": the
        reference is that project's image, its already-registered sources are
        dropped, and nothing about naming or datasets is proposed.

    A `node://<node>/<resource>` among the paths is inspected by the machine
    that can open it and MIXED with the rest, because that is the arrangement
    people actually have: the slide beside the viewer and the cell table still
    on the laptop, or the other way round. Each resource's location is an
    independent fact (see `Project.resources`), so nothing here requires them
    all to be in one place.
    """
    answers = dict(answers or {})
    paths = list(paths or [])
    proposal = Proposal()
    if not any(str(p).strip() for p in paths):
        return proposal

    found, questions, bundles, warnings = [], [], [], []
    # ENUMERATED, and blanks skipped INSIDE the loop rather than filtered out
    # of the list first. `index` is the caller's own position in the array it
    # sent, and the screen removes a row by that position -- so a filter here
    # would renumber every pick after the first empty entry and a Remove would
    # take out the wrong file.
    for index, raw in enumerate(paths):
        if not str(raw).strip():
            continue
        declared = _declared(raw, answers)
        mine = _answers_for(answers, raw)
        if _is_node_address(raw) or node:
            layers, path_questions, bundle, path_warnings = _detect_node(
                raw, node, mine, declared)
        else:
            layers, path_questions, bundle, path_warnings = _detect(
                raw, mine, declared)
        for question in path_questions:
            question.pick = index
            question.id = _per_pick(question.id, raw)
        if not layers:
            proposal.unrecognised.append({
                "pick": index,
                "path": str(raw),
                "reason": (path_warnings[0] if path_warnings else
                           "Nothing Plexora can read here."),
            })
            continue
        attach = _attached(raw, answers)
        for layer in layers:
            layer.pick = index
            layer.attach = attach
            layer.said = str(raw).strip()
            layer.needs = tuple(_per_pick(need, raw) for need in layer.needs)
            if layer.switch:
                # Answered under the same per-pick id its question would be.
                layer.switch = {**layer.switch, "question": _per_pick(
                    layer.switch["question"], raw)}
        found.extend(layers)
        questions.extend(path_questions)
        warnings.extend(path_warnings)
        if bundle is not None and bundle not in bundles:
            bundles.append(bundle)

    proposal.warnings.extend(warnings)
    panels = [layer for layer in found if layer.role == "channels"]
    found = _without_image_copies(
        [layer for layer in found if layer.role != "channels"])
    if panels and (sample or not found):
        # A channel list names an image's channels; on its own, or added to a
        # sample that already has its names, there is nothing for it to name.
        for panel in panels:
            proposal.unrecognised.append({
                "pick": panel.pick, "path": str(panel.said or panel.src),
                "reason": (f"{Path(panel.src).name} lists channel names. Pick "
                           "it with the images it names, or rename a sample's "
                           "channels from the viewer."),
            })
    if not found:
        return proposal

    if sample:
        scoped = _scoped_sample(sample, found, questions, bundles)
        proposal.samples.append(scoped)
        _choose_subsets([scoped], answers, scoped_image=_project_image(sample))
        return proposal

    for group in _split_samples(found, bundles, questions, answers, proposal):
        proposal.samples.append(group)
    _name_channels(proposal.samples, panels, proposal.warnings)
    _choose_subsets(proposal.samples, answers)
    return proposal


def _project_image(name):
    """The `src` of an existing sample's image, for naming what it is of."""
    from plexora.server.models.project import Project

    try:
        return Project.load(name).image.src or ""
    except Exception:  # noqa: BLE001
        return ""


def _name_channels(samples, panels, warnings):
    """Give each sample's image the names a picked channel list holds.

    Applied to every sample, never asked about: a run's `markers.csv` is the
    panel of every slide in it. Each image takes the list nearest it -- in its
    own folder, else the closest folder above -- or the only list picked. One
    whose length is not the image's channel count is not applied, and said so;
    the image keeps the names it carries itself.
    """
    if not panels:
        return
    for sample in samples:
        reference = next((layer for layer in sample.layers
                          if layer.reference and layer.kind == "image"), None)
        if reference is None or (reference.render or {}).get("rgb"):
            continue
        count = int((reference.geometry or {}).get("numChannels") or 0)
        panel = _panel_for(reference, panels)
        if panel is None or count <= 0:
            continue
        sheet = Path(panel.src).name
        names = _panel_names(panel.src, count)
        if names is None:
            warnings.append(
                f"{sheet} does not list {count} channel names, so "
                f"{reference.label} keeps the names it carries.")
            continue
        reference.channel_names = tuple(names)
        # Last, after the calibration: the row is one line and clips, and the
        # size and pixel size are what a person checks first.
        reference.detail = f"{describe(reference)} · channel names from {sheet}"


def _panel_for(image, panels):
    """The channel list for one image: the nearest one at or above its folder,
    else the only one picked, else None."""
    best, best_distance = None, None
    if image.src and not _is_node_address(image.src) \
            and not is_remote_locator(image.src):
        folder = Path(image.src).parent
        for panel in panels:
            home = Path(panel.src).parent
            if home == folder:
                distance = 0
            elif home in folder.parents:
                distance = len(folder.parts) - len(home.parts)
            else:
                continue
            if best_distance is None or distance < best_distance:
                best, best_distance = panel, distance
    if best is None and len(panels) == 1:
        best = panels[0]
    return best


def _without_image_copies(found):
    """`found` less any pyramidized copy whose original is also in it.

    A folder holding `slide.tif` and the `slide.pyramid.ome.tiff` Plexora
    made of it is one image: registering the original adopts the copy. Two
    rows would be two references with one id. Suffix first, so the metadata
    read is paid only by files that could be a copy.
    """
    from plexora.server.utils import image_pyramid

    sources = {str(layer.src) for layer in found if layer.src}
    keep = []
    for layer in found:
        src = str(layer.src or "")
        if (layer.role == "image"
                and src.lower().endswith(image_pyramid.PYRAMID_SUFFIX)
                and image_pyramid.copy_source(src) in sources):
            continue
        keep.append(layer)
    return keep


def _unsettled(layer) -> bool:
    """Whether this row is still waiting to be told whether it is a mask."""
    return any(str(need).startswith("mask-or-image:") for need in layer.needs)


def _split_samples(found, bundles, questions, answers, proposal):
    """One sample, or several, out of what was detected.

    A bundle is always one sample. Loose files sharing a stem are one sample.
    Several raster images with different stems is the only genuinely ambiguous
    case, and it is the one question asked -- defaulting to separate samples,
    because two slides in a folder are usually two slides.

    Two things run before that. Picks the user attached to a named sample are
    held back, because they are not candidates for anything: they were added
    to a card. And an image that could equally be a mask does not get to
    ANCHOR a sample while a certain image is standing beside it -- which is
    what turned "a slide and its segmentation" into two cards, the single
    plainest complaint about this screen. Its question survives, under its own
    row, still defaulting to `image`: nothing here decides what a file is.
    """
    if len(bundles) > 1:
        return _split_bundles(found, bundles, questions, proposal)
    if bundles:
        return [_assemble(_sample_name([b["root"] for b in bundles], bundles),
                          found, questions, bundles)]

    held = [l for l in found if l.attach]
    rest = [l for l in found if not l.attach]
    if not rest:
        # Everything was attached to a sample that is not in this set -- all
        # that is left is the files themselves.
        held, rest = [], held

    images = [l for l in rest if l.kind == "image" and l.role == "image"]
    certain = [l for l in images if not _unsettled(l)]
    if certain and len(certain) < len(images):
        images = certain
    # Several images are several samples, and nothing asks. There used to be
    # a question here -- "N images. Import as separate samples, or as layers
    # of one?" -- repeated on every card, and it duplicated what the screen
    # already says with its structure: a file added through a card's own
    # "+ Add layer" is a layer of that card (it is `held`, above, and never
    # reaches this list), and a file picked on its own is a sample of its own.
    #
    # `images-grouping: layers` is still honoured when a script passes it --
    # `inspect_paths` is public, and a notebook that relied on it should not
    # quietly start making N projects. It is never asked.
    grouping = answers.get("images-grouping") or "separate"
    samples, orphans = [], []
    # One anchor per (stem, folder). The folder is what tells two samples'
    # `image.ome.tif` apart -- by stem alone they were one group, and every
    # same-stemmed row landed in both samples, or both images in one. Same
    # stem in the SAME folder is still one sample, as it always was:
    # `LSP11641.ome.tif` beside `LSP11641_he.tif` is a slide and its H&E.
    anchors = {}
    for image in images:
        anchors.setdefault((_group_stem(_named_by(image)), _folder_of(image)),
                           []).append(image)
    if len(anchors) > 1 and grouping == "separate":
        shared = {}
        for stem, _folder in anchors:
            shared[stem] = shared.get(stem, 0) + 1
        claimed = set()
        for (stem, folder), group in anchors.items():
            srcs = {str(image.src) for image in group}
            if shared[stem] > 1:
                # The stem names several samples, so it cannot say which of
                # them anything else belongs to: only what sits in the same
                # folder joins, and the rest are placed like any loose file.
                mine = [l for l in rest if l.src and id(l) not in claimed
                        and (l.found_for in srcs if l.found_for else
                             _group_stem(_named_by(l)) == stem
                             and _folder_of(l) == folder)]
            else:
                # A row found FOR one of these images joins by that, and only
                # by that: its own stem is a folder's choice of filename.
                mine = [l for l in rest if l.src and id(l) not in claimed
                        and (l.found_for in srcs if l.found_for else
                             _group_stem(_named_by(l)) == stem)]
            claimed.update(id(l) for l in mine)
            sample = _assemble(_clean_name(stem), mine, questions, [])
            if shared[stem] > 1:
                sample.distinct = folder
                sample.key = _sample_key(sample)
            samples.append(sample)
        orphans = [l for l in rest if id(l) not in claimed]

    if not samples:
        # Named by what was picked, not by what was found for it: a mask out
        # of `mccellpose-LSP11385/` is called `cell`, which is not the slide.
        samples = [_assemble(_sample_name([_named_by(l) for l in rest
                                           if l.src and not l.found_for],
                                          bundles),
                             rest, questions, bundles)]
    _place(samples, held, orphans, proposal, answers)
    if len(samples) > 1:
        # Every card was handed every question, so "which image in this
        # store?" about sample 2's pick was drawn on sample 1's card too, and
        # answered there. Each card keeps the questions its own picks asked;
        # one no pick asked (`sample-for`, from `_place`) was only ever given
        # to the card it is about. A folder is ONE pick holding several
        # samples, so a question that says which row it is about goes only
        # where that row is.
        for sample in samples:
            picks = {layer.pick for layer in sample.layers}
            srcs = {str(layer.src) for layer in sample.layers if layer.src}
            sample.questions = [q for q in sample.questions
                                if (q.pick is None or q.pick in picks)
                                and (q.about is None or q.about in srcs)]
    return samples


def _split_bundles(found, bundles, questions, proposal):
    """Several run folders: one sample each, and nothing shared between them.

    They used to be assembled into ONE sample -- two Xenium runs added as two
    samples came back as a single card named after the first, holding one
    run's cell table and one set of transcripts, the other run's rows gone
    without a word (both carried the same layer ids). A run is a sample; two
    runs are two.

    A run's own rows join its sample by the bundle they came out of. Anything
    else -- a mask or table added on a card, or picked loose -- is placed the
    way loose files always are: by the card it was added on, then by name,
    then by a question.
    """
    samples, claimed = [], set()
    for bundle in bundles:
        root = bundle.get("root")
        mine = [l for l in found
                if l.bundle and l.bundle.get("root") == root]
        claimed.update(id(l) for l in mine)
        picks = {l.pick for l in mine}
        samples.append(_assemble(_sample_name([root], [bundle]), mine,
                                 [q for q in questions if q.pick in picks],
                                 [bundle]))
    rest = [l for l in found if id(l) not in claimed]
    held = [l for l in rest if l.attach]
    orphans = [l for l in rest if not l.attach]
    _place(samples, held, orphans, proposal)
    # A loose row's own questions go where the row went.
    for sample in samples:
        picks = {l.pick for l in sample.layers if id(l) not in claimed}
        srcs = {str(l.src) for l in sample.layers if l.src}
        sample.questions.extend(q for q in questions
                                if q.pick in picks and q not in sample.questions
                                and (q.about is None or q.about in srcs))
    return samples


def _place(samples, held, orphans, proposal, answers=None):
    """Put the rows that anchored nothing into the sample they belong to.

    Three ways in, in descending order of how much is known:

    1. The user said so, on a card: `attach` carries that sample's `key` and
       the row goes there. If the sample it named is gone -- its image was
       removed after the mask was added to it -- the row falls through to (2)
       rather than vanishing.
    2. Its filename says so. `LSP11641_cells.csv` beside `LSP11641.ome.tif` is
       that slide's table, and this is the same claim `_group_stem` already
       makes; what is new is that it is made on a PREFIX, so `cells_lsp11641`
       and `mask-LSP11641-v2` land too. Only when one sample wins outright.
    3. Nothing says so, and there is more than one sample it could be. That is
       a question, asked on the card of the sample it provisionally joined --
       which is the first one, so the default never leaves a file out.

    A row out of a picked FOLDER is asked about under its own file, not the
    folder: the folder is one pick holding every sample's files, and "the
    folder belongs to sample 2" is not what anybody answered. That answer is
    read here, and the question stays on the card it moved the row to, so it
    can be changed back.
    """
    answers = answers or {}
    by_key = {sample.key: sample for sample in samples if sample.key}
    loose = []
    touched = set()
    for layer in held:
        target = by_key.get(layer.attach)
        if target is None:
            loose.append(layer)
            continue
        target.layers.append(layer)
        touched.add(id(target))

    for layer in list(orphans) + loose:
        shared = _cohort_targets(layer, samples) if len(samples) > 1 else None
        if shared:
            # A table that spans several images, picked with them: each sample
            # it covers reads its own image's cells out of it. Asking which ONE
            # sample it belongs to was asking the wrong question.
            for target, value in shared:
                copy_ = replace(layer, subset={**layer.subset, "value": value},
                                needs=tuple(layer.needs))
                target.layers.append(copy_)
                touched.add(id(target))
            continue
        target = _best_sample(layer, samples)
        if target is None:
            name = Path(_named_by(layer)).name
            inside = _from_inside_a_folder(layer)
            # Filed under the pick, not the filename: two samples' loose
            # `cellRing.ome.tif` are two questions with two answers.
            question = Question(
                id=f"sample-for:{layer.src if inside else (layer.said or name)}",
                label=f"Which sample does {name} belong to?",
                kind="select",
                options=tuple({"value": s.key, "label": s.name}
                              for s in samples),
                default=samples[0].key,
                scope=f"layer:{layer.id}")
            target = (by_key.get(answers.get(question.id)) if inside
                      else None) or samples[0]
            target.questions.append(question)
            layer.needs = tuple(layer.needs) + (question.id,)
        target.layers.append(layer)
        touched.add(id(target))

    for sample in samples:
        if id(sample) in touched:
            _finish(sample)
        # One DataSpec per sample: `register_sample` takes the first row whose
        # role is `table` and the rest are not written anywhere. Said out
        # loud, because a second cell table silently disappearing is a project
        # that reads as imported and is missing half its data.
        tables = [l for l in sample.layers if l.role == "table"]
        for extra in tables[1:]:
            proposal.warnings.append(
                f"{sample.name} can hold one cell table, so "
                f"{Path(_named_by(extra)).name} is left out.")


def _cohort_targets(layer, samples):
    """`[(sample, value)]` for a multi-image table whose image column names
    more than one of these samples' images, else None."""
    if layer.role != "table" or not layer.subset:
        return None
    values = list(layer.subset.get("values") or [])
    found, taken = [], set()
    for sample in samples:
        image = next((l for l in sample.layers
                      if l.kind == "image" and l.role == "image"), None)
        value = subset_match(values, _image_names(image.src if image else None,
                                                  sample.name))
        if value is not None and value not in taken:
            taken.add(value)
            found.append((sample, value))
    return found if len(found) > 1 else None


def _shared_prefix(left, right) -> int:
    count = 0
    for a, b in zip(left, right):
        if a != b:
            break
        count += 1
    return count


def _from_inside_a_folder(layer) -> bool:
    """Whether this row is one file out of a picked folder, rather than the
    file that was picked. Local paths only: a node's rows carry a derived id,
    and a web address is always the thing that was picked."""
    src, said = str(layer.src or ""), str(layer.said or "")
    if not src or not said or _is_node_address(src) or is_remote_locator(src):
        return False
    try:
        return Path(src).resolve() != Path(said).expanduser().resolve()
    except OSError:
        return False


def _starts_with_word(stem, prefix) -> bool:
    """Whether `stem` begins with the whole of `prefix`, followed by nothing
    or by a separator: `lsp11385--mccellpose_cell` begins with the word
    `lsp11385`, and not with `lsp11385_rv`, which shares as many letters."""
    return bool(prefix) and stem.startswith(prefix) and (
        len(stem) == len(prefix) or not stem[len(prefix)].isalnum())


def _best_sample(layer, samples):
    """The one sample whose stem this row's filename matches, or None.

    A row found for one image (`found_for`) goes to that image's sample. Then
    a sample whose whole name LEADS the filename, as a word: mcmicro writes
    `LSP11385--mccellpose_cell.csv` beside `LSP11385_rv.ome.tif` too, and by
    shared letters alone the two slides tied and the table was asked about.
    The longest such name wins, so `LSP11385_rv--…` is the `_rv` slide's.

    Otherwise the longest shared prefix, four characters at least, because two
    and three match half a study: `ls` is every slide in an mcmicro run.
    Outright, because a row that matches two samples equally well is exactly
    the row a person has to decide about.
    """
    if len(samples) == 1:
        return samples[0]
    if layer.found_for:
        owner = next((sample for sample in samples
                      if any(str(other.src) == layer.found_for
                             for other in sample.layers)), None)
        if owner is not None:
            return owner
    stem = _group_stem(_named_by(layer))
    if not stem:
        return None
    worded = sorted(((len(_stem_of(sample)), sample) for sample in samples
                     if _starts_with_word(stem, _stem_of(sample))),
                    key=lambda pair: pair[0], reverse=True)
    if worded and (len(worded) == 1 or worded[1][0] < worded[0][0]):
        return worded[0][1]
    ranked = sorted(
        ((_shared_prefix(stem, _stem_of(sample)), sample)
         for sample in samples),
        key=lambda pair: pair[0], reverse=True)
    best = ranked[0][0]
    if best < 4 or ranked[1][0] == best:
        return None
    return ranked[0][1]


def _stem_of(sample) -> str:
    """The stem a sample is known by: its reference's filename, or its first."""
    reference = next((l for l in sample.layers if l.reference), None)
    reference = reference or next((l for l in sample.layers if l.src), None)
    return _group_stem(_named_by(reference)) if reference else ""


def _sample_key(sample) -> str:
    """What a sample IS, from its contents rather than its position.

    A card's "+ Add segmentation mask" has to name the sample the file joins,
    and the list is rebuilt from scratch on every inspection -- so an index
    would mean a file added to the second card landing on whichever sample was
    second by the time the answer came back. This is derived from the files
    themselves, so removing another pick, answering a question or adding a
    third sample leaves it alone.
    """
    if sample.bundles:
        root = str(sample.bundles[0].get("root") or "")
        try:
            root = str(Path(root).resolve())
        except OSError:
            pass
        return "bundle:" + root
    stem = _stem_of(sample)
    if stem and sample.distinct:
        # Two samples share this stem; the folder is what each one IS.
        return f"stem:{stem}@{sample.distinct}"
    if stem:
        return ("frame:" if sample.frame else "stem:") + stem
    return "sample:" + sample.name


def _folder_of(layer) -> str:
    """The folder a row's file sits in, as text, for telling two same-named
    samples apart. The pick for a node file (its `src` is a derived resource
    id, which has no folder); the file itself for anything local."""
    text = str(layer.src or "")
    if not text or _is_node_address(text):
        text = str(layer.said or text)
    text = text.replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[0] if "/" in text else ""


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
               run_pixel_size,
               frame_scale=((reference.render or {}).get("frameScale")
                            if reference is not None else None))

    if reference is not None and reference.frame == "fullres":
        reference.frame = None
    if reference is not None and not reference.pixel_size:
        # A full-res H&E that states no calibration, picked with a Visium HD
        # run: the run states it for that very frame.
        reference.pixel_size = next(
            (b.get("fullres_pixel_size") for b in sample.bundles
             if b.get("fullres_pixel_size")
             and not (reference.render or {}).get("frameScale")), None)

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

    # Both derived from the finished layer list, and both re-derived whenever
    # it changes -- which is why this is here and not in `_assemble`.
    sample.key = _sample_key(sample)
    roles = {layer.role for layer in sample.layers}
    sample.missing = tuple(role for role in ("mask", "table")
                           if role not in roles)


def _identity(src) -> str:
    """One spelling per source, for telling "already in this sample" apart:
    the canonical URL of a web address, the resolved path of a file."""
    if is_remote_locator(src):
        from plexora.server.utils import remote_store

        return remote_store.canonical_url(src)
    return str(Path(src).resolve())


def _scoped_sample(name, found, questions, bundles):
    """"+ Add Layer": everything is a layer of a sample that already exists."""
    from plexora.server.models.project import Project

    try:
        project = Project.load(name)
    except KeyError:
        project = None

    registered = {_identity(l.src) for l in (project.spatial_layers if project else ())
                  if l.src}
    if project is not None and project.image.src:
        registered.add(_identity(project.image.src))

    layers = []
    reference_pixel_size = ((project.image.pixel_size or {}).get("value")
                            if project else None)
    run_pixel_size = next((l.pixel_size for l in found
                           if l.transform_source == "run" and l.pixel_size), None)
    # What the project's reference is of a Visium run's full-res frame, as
    # recorded on the run's bundle when it was imported -- a bins layer added
    # later is otherwise registered against the full-res frame and lands
    # seven times too big over a hires PNG.
    frame_scale = next((b.get("frameScale") for b in
                        (project.bundles if project else ())
                        if b.get("frameScale")), None)
    for layer in found:
        try:
            if layer.src and _identity(layer.src) in registered:
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
            _align(layer, None, reference_pixel_size, run_pixel_size,
                   frame_scale=frame_scale)
        layers.append(layer)

    sample = SampleProposal(name=name, layers=layers, questions=list(questions),
                            bundles=[dict(b) for b in bundles])
    # The project IS the key here -- there is only ever one sample on this
    # screen and it already has a name. What it still lacks comes from the
    # record rather than from the picks, because the picks are what is being
    # added TO it.
    sample.key = name
    have = {layer.role for layer in layers}
    if project is not None:
        if project.segmentation.requested:
            have.add("mask")
        if project.has_data_source:
            have.add("table")
    sample.missing = tuple(role for role in ("mask", "table")
                           if role not in have)
    return sample


def _is_node_address(value) -> bool:
    return str(value).startswith("node://")


def _looks_like_a_path(value) -> bool:
    """Whether a node address's second half is a path rather than a resource id.

    `node://<node>/<resource>` carries a resource id -- the name the node
    serves something under. What the import screen puts there is the path the
    user just browsed to on that machine, because at that moment nothing has
    told the node to serve it and there is no id yet.

    A separator is the whole test, and it is a sound one: an id derived by
    `nodes.resource_id_for` is a slug and a hash with nothing else in it, and
    an id named by `--serve kind:id=path` cannot hold a slash either, since
    that is the syntax's own punctuation. Both separators, and a drive letter,
    because the far side is as likely to be a Windows workstation as a
    cluster.
    """
    text = str(value)
    return ("/" in text or "\\" in text or text.startswith("~")
            or (len(text) > 2 and text[1] == ":"))


def _bound_project(node, resource_id):
    """The name of a project already reading this exact resource, or None.

    Asked before a resource is re-served under a different kind. The file the
    user is correcting is nearly always one this same screen shared seconds
    ago and nothing else has ever seen -- but if a project IS reading it, the
    correction would pull the image out from under that project, which is a
    much larger act than the answer to a mask-or-image question.
    """
    from plexora import get_config
    from plexora.server.models.project import Project

    for name, entry in (get_config() or {}).items():
        try:
            bindings = Project.from_entry(name, entry).resources
        except Exception:
            continue
        for binding in (bindings or {}).values():
            if (getattr(binding, "node", None) == node
                    and str(getattr(binding, "resource_id", "")) == str(resource_id)):
                return name
    return None


def _serve_on_node(node, path, served, answers, declared=None):
    """Have a node start serving a path the user browsed to, and describe it.

    `(resource_id, described, questions, name, warnings)`. The step the screen is
    missing without this: a browse hands back a path, and a path means nothing
    to anything here until the machine holding it has been asked to serve it
    -- which is the same thing a data field on the landing page does the
    moment somebody picks a file on another machine.

    The kind comes from the node (`/node/v1/detect`), not from the filename.
    `cell.ome.tif` out of an mcmicro run is a segmentation mask and its name
    says so nowhere; the plane count and dtype that do say so are readable
    only over there.
    """
    from plexora import nodes as node_api

    detected = node_api.detect_on_node(node, path)
    name = str(detected.get("name") or path)
    kind = detected.get("kind")
    if not kind:
        raise ValueError(detected.get("reason")
                         or f"{node} cannot read {name}.")

    questions, warnings = [], []
    if kind in ("image", "segmentation"):
        # The same question, with the same id, that a file on this server's
        # own disk gets when the pixels are consistent with both readings --
        # so an answer given on the card flows back through `answers` here
        # exactly as it does there. And the same order of precedence: the
        # answer wins over the action the file arrived through, because the
        # answer is later and names this file.
        whole = f"node://{node}/{path}"
        answer = (answers.get(f"mask-or-image:{whole}")
                  or answers.get(f"mask-or-image:{name}"))
        if answer:
            kind = "segmentation" if answer == "mask" else "image"
        elif declared == "mask" and detected.get("mask") is None:
            kind = "segmentation"
        elif declared == "mask" and detected.get("mask") is False:
            kind = "image"
            warnings.append(
                f"{name} was added as a segmentation mask, but its pixels are "
                "an image -- proposed as an image instead.")
        elif detected.get("mask") is None:
            questions.append(Question(
                id=f"mask-or-image:{whole}",
                label=f"Is {name} a segmentation mask or an image?",
                options=({"value": "image", "label": "An image"},
                         {"value": "mask", "label": "A segmentation mask"}),
                default="image"))
            kind = "image"

    resource_id = node_api.resource_id_for(path)
    described = served.get(str(resource_id))
    if described is not None and str(described.get("kind")) != kind:
        owner = _bound_project(node, resource_id)
        if owner:
            raise ValueError(
                f"{node} serves {name} as the {described.get('kind')} of the "
                f"project {owner!r}. Import it there, or copy the file.")
        node_api.unshare_path(node, resource_id)
        described = None
    if described is None:
        described = node_api.share_path(node, kind, path)
        with _SHARED_LOCK:
            _SHARED_BY_INSPECTION.add((str(node), str(resource_id)))
    return resource_id, dict(described), questions, name, warnings


#: `(node, resource_id)` for every file THIS server asked a node to serve
#: while reading picks for the import dialog -- before anything was imported.
#: Inspection has to share a browsed path to learn what it is, so a mask that
#: is picked and then removed, or a dialog that is closed without importing,
#: used to leave the file registered on the node indefinitely (its manifest
#: survives restarts), under whatever kind it was last read as. That orphan
#: is what later refused the same file from the viewer.
#:
#: Only these are ever released: a resource that was already being served
#: when the dialog looked at it belongs to something else.
_SHARED_BY_INSPECTION: set = set()
_SHARED_LOCK = threading.Lock()


def release_picks(paths) -> list:
    """Stop a node serving what inspecting these picks shared, if unused.

    Called when the dialog drops picks -- a row removed, the dialog closed --
    so what is left registered on a node is exactly what some project reads.
    A resource a project binds is kept, whoever shared it: that is the import
    having succeeded. Returns the `node://` addresses released.

    Best effort per resource, because it runs on the way out of a dialog and
    a node that is unreachable right now has nothing to be told; the entry is
    kept so a later release can try again.
    """
    from plexora import nodes as node_api
    from plexora.server.routes.import_routes import _node_locator

    released = []
    for raw in paths or []:
        try:
            located = _node_locator(str(raw))
        except ValueError:
            continue
        if not located:
            continue
        node, target = located
        resource_id = (node_api.resource_id_for(target)
                       if _looks_like_a_path(target) else str(target))
        entry = (str(node), str(resource_id))
        with _SHARED_LOCK:
            if entry not in _SHARED_BY_INSPECTION:
                continue
        if _bound_project(node, resource_id):
            with _SHARED_LOCK:
                _SHARED_BY_INSPECTION.discard(entry)
            continue
        try:
            node_api.unshare_path(node, resource_id)
        except Exception:
            continue
        with _SHARED_LOCK:
            _SHARED_BY_INSPECTION.discard(entry)
        released.append(f"node://{node}/{resource_id}")
    return released


def _detect_node(raw, fallback_node, answers, declared=None):
    """One resource a data node is serving, as a layer of this sample.

    A node is the only process that can open its own filesystem, so what a
    resource IS comes from the node's own description rather than from a second
    detector here. The address is carried through AS THE `src`, because
    `attach_segmentation` and `replace_project_data` already dispatch on it --
    which is what keeps a node-backed mask and a local one one code path rather
    than two that drift.

    Bundles are not proposed from here: enumerating a run directory needs a
    listing this side does not have, and saying so is better than
    half-detecting it.
    """
    from plexora import nodes as node_api
    from plexora.server.routes.import_routes import _node_locator

    try:
        located = _node_locator(raw)
    except ValueError as error:
        return [], [], None, [str(error)]
    if located is None:
        if not fallback_node:
            return [], [], None, [str(raw) + " is not a node address."]
        located = (fallback_node, str(raw))
    node, resource_id = located

    try:
        served = {str(entry.get("id")): entry
                  for entry in node_api.node_resources(node)}
    except KeyError:
        return [], [], None, [
            "No data node named " + repr(node) + " is registered here."]
    except Exception as error:
        return [], [], None, [
            "The node " + repr(node) + " could not be reached: " + str(error)]

    described = served.get(str(resource_id))
    kind_questions, picked, kind_warnings = [], None, []
    #: The path as the user browsed to it -- what `_serve_on_node` files the
    #: mask-or-image answer under, before the node turns it into an id.
    browsed = str(resource_id)
    if described is None and _looks_like_a_path(resource_id):
        try:
            (resource_id, described, kind_questions, picked,
             kind_warnings) = _serve_on_node(
                node, str(resource_id), served, answers, declared)
        except ValueError as error:
            # Said by us, about something the user can act on: a folder, a
            # file nothing reads, a resource another project owns.
            return [], [], None, [str(error)]
        except Exception as error:
            # Said by the node, and already naming itself -- see
            # `providers/http._check`, which is also what turns a node too old
            # to have `/detect` into one sentence about upgrading it.
            return [], [], None, [str(error)]
    if described is None:
        return [], [], None, [
            node + " is not serving " + repr(resource_id) + "."]

    address = "node://" + node + "/" + str(resource_id)
    kind = described.get("kind")
    # The filename when the user picked a file, the id when they named a
    # resource: whichever of the two they would recognise on the row.
    shown = picked or str(resource_id)
    label = node + " / " + shown
    layer_id = _layer_id(shown) if picked else _clean_name(resource_id)

    if kind == "image":
        # No dimensions on the row, and deliberately. `/hello` reports what a
        # node knows without opening anything -- the kind, the state, the image
        # type it worked out when the resource was added -- and the geometry
        # comes from a second endpoint that walks the pyramid. This runs while
        # somebody is still picking, so it asks nothing that costs a pyramid
        # walk; registration asks, once, because by then it has to.
        layer = LayerProposal(
            id=layer_id, kind="image", role="image",
            reference=True, label=label, src=address, filename=picked,
            modality=("he" if described.get("image_type") == "brightfield"
                      else "multiplex"),
            needs=tuple(q.id for q in kind_questions),
            render={"detail": "on " + node})
        if picked and layer.modality != "he":
            layer.switch = _role_switch(
                f"mask-or-image:node://{node}/{browsed}", "image")
        questions = list(kind_questions)
        pyramid = described.get("pyramid")
        if pyramid:
            # The same offer a flat image on this disk gets, built over there:
            # the original is on that machine, so that is where the copy is
            # written. Asked by address, which is what registration reads.
            layer.render = {
                "detail": "on " + node + " · No pyramid",
                "pyramid": {**pyramid, "location": (
                    f"on {node}, beside the original (or in the node's own data "
                    "folder if that one is not writable)")},
            }
            questions.append(Question(
                id=f"pyramidize:{address}",
                label="Build a pyramidized copy before opening?",
                options=({"value": "yes", "label": "Pyramidize and import"},
                         {"value": "no", "label": "Import as is"}),
                default="yes", scope=f"layer:{layer.id}", kind="confirm"))
        layer.detail = describe(layer)
        return [layer], questions, None, kind_warnings

    if kind == "segmentation":
        # A mask shared this minute may still be becoming a label pyramid over
        # there, and on a whole-slide mask that is minutes. Said on the row,
        # because the project registers either way and the cell layer is
        # simply not there until the conversion lands -- which without this
        # reads as a mask that failed rather than one that is coming.
        state = str(described.get("state") or "")
        layer = LayerProposal(
            id=layer_id, kind="labels", role="mask",
            modality="mask", label=label, src=address, filename=picked,
            needs=tuple(q.id for q in kind_questions),
            render={"detail": ("converting on " + node if state == "preparing"
                               else "could not be prepared on " + node
                               if state == "error" else "on " + node)})
        if picked:
            layer.switch = _role_switch(
                f"mask-or-image:node://{node}/{browsed}", "mask")
        layer.detail = describe(layer)
        return [layer], kind_questions, None, kind_warnings

    if kind == "table":
        questions = list(kind_questions)
        try:
            document = node_api.inspect_table(node, resource_id,
                                              table=answers.get("table"))
        except Exception as error:
            return [], [], None, [
                "The node " + repr(node) + " could not inspect "
                + repr(resource_id) + ": " + str(error)]
        tables = list(document.get("tables") or [])
        chosen = answers.get("table")
        names = [entry.get("name") for entry in tables]
        table_name = (chosen if chosen in names
                      else (names[0] if len(names) == 1
                            else document.get("table")))
        layer = LayerProposal(
            id="table", kind="table", role="table", modality="cells",
            label=label, src=address, filename=picked, table=table_name,
            render={"detail": document.get("data_type") or ("on " + node)})
        # The node inspected it the way this side inspects a local file, so a
        # table spanning several images is matched to its image the same way
        # (`_choose_subsets`); `attach_table` takes the answer on to the node.
        ambiguous = (document.get("proposed") or {}).get("ambiguous") or []
        for column in ambiguous:
            values = next((list(entry.get("values") or []) for entry in
                           document.get("obs_columns") or []
                           if entry.get("name") == column), [])
            if len(values) > 1:
                layer.subset = {"column": column, "values": values}
                break
        if len(tables) > 1:
            questions.append(Question(
                id="table", scope="layer:table",
                label="Which table holds the cells?", kind="select",
                options=tuple({"value": name, "label": name} for name in names),
                default=table_name))
            if not table_name:
                layer.needs = ("table",)
        layer.detail = describe(layer)
        return [layer], questions, None, kind_warnings

    return [], [], None, [
        node + " serves " + repr(resource_id) + " as " + repr(kind)
        + ", which Plexora does not draw."]
