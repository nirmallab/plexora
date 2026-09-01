"""Reading brightfield / H&E slides, and telling them apart from fluorescence.

Plexora's image pipeline was built for multiplex fluorescence: N grayscale
planes, one tile request per plane, coloured and composited additively on black
in the browser. An H&E slide is the opposite shape -- one *colour* image whose
three samples are not three biological channels but the red, green and blue a
camera recorded through a white light. Serving it as three channels produces a
viewer that is wrong in a way nobody can un-see: R/G/B offered as markers, a
black background, and additive blending that turns pale eosin into noise.

Two problems, handled here rather than at the call sites:

**Which kind is this file?** `detect_image_type` answers it the way the tools
that have had to answer it for twenty years do -- structurally first, pixels
last. Storage layout is the strongest single signal (Bio-Formats' `isRGB()` is
exactly "one plane carries several samples"), OME-XML's per-channel
`SamplesPerPixel`/`ContrastMethod` is stronger still when it is present at all,
and only when the file says nothing structural does it fall back to QuPath's
thumbnail heuristic (a light background is transmitted light; a dark one is
emitted). **Never on channel count alone**: a genuine 3-plex fluorescence panel
exists and is common, and calling it H&E because it has three planes would be
the single worst failure this module could have.

**How is it read?** `open_rgb` returns an `RgbPyramid` shaped exactly like the
zarr *group* tifffile hands back for a pyramidal TIFF -- `pyramid[str(level)]`,
`len(pyramid)`, each level indexed `[channel, rows, cols]` -- so `_zarr_level`,
`read_tile`'s isinstance branches and `node/api.py`'s `hasattr(pyramid,
"shape")` test all take their existing paths untouched. Each level *also*
exposes `.rgb[rows, cols]`, which is the one seam where colour leaves this
module: `read_tile` hands those bytes straight to a WebP encoder. Everything
else in Plexora keeps seeing (channel, y, x), which is what makes the
"Fluorescence" override of an RGB file honest rather than special-cased -- the
same pyramid, read as three planes, through code that was never told.

**Levels are virtual.** A whole-slide image's own pyramid is whatever the
scanner wrote: Aperio steps by 4, some vendors by 2, some files have no pyramid
at all. The viewer's tile source computes a level's size as `size >> level`, so
the levels it asks for are always halvings. Rather than convert a 300 MB slide
at import to make those exist, `open_rgb` presents the full halving chain and
each virtual level reads from the nearest native one, downsampling the region
in flight -- which for the 4x pyramid an SVS actually has means one extra 2x
reduction on two of the eight levels, and no conversion at all. A level whose
nearest native source is too far away (an image written with no pyramid, where
a zoomed-out tile would decode the better part of a gigabyte) is where that
stops: those levels are derived once into the project directory by the same
`ome_zarr.build_extension` the NGFF path uses, because the CYX view above is
already the input contract it wants.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

#: The two answers. `image_kind` carries the first one verbatim; the second is
#: never an `image_kind` (a fluorescence image is `ome_tiff`/`ome_zarr`, named
#: for the container it came out of) and only ever appears in a detection
#: verdict or a user's override.
BRIGHTFIELD = "brightfield"
FLUORESCENCE = "fluorescence"

#: The channel key a brightfield image's tiles are requested under:
#: `/generated/data/<ds>/rgb/<level>/<x>_<y>.png`. A sentinel rather than an
#: index, because there is no index to name -- the tile carries all three
#: samples. `data_model._parse_channel` special-cases it FIRST, before the
#: `_<N>` regex, which would otherwise fall through to its segmentation branch.
RGB_CHANNEL_KEY = "rgb"

#: Containers that hold nothing but brightfield. OpenSlide reads these and has
#: no concept of a channel at all, so the format itself is the evidence -- tier
#: one of the ladder, and the only tier that never opens the file.
BRIGHTFIELD_ONLY_SUFFIXES = (".svs", ".ndpi", ".scn", ".mrxs", ".bif", ".svslide")

#: Formats only OpenSlide can open -- everything else in the list above is a
#: TIFF underneath and tifffile reads it directly.
OPENSLIDE_ONLY_SUFFIXES = (".mrxs", ".svslide")

#: Suffixes quick view and the import wizard accept as "an image, let the
#: conversion decide which kind". Kept here rather than in `datasource.py` so
#: the sniffer and the reader cannot drift apart.
WSI_SUFFIXES = BRIGHTFIELD_ONLY_SUFFIXES

#: The virtual tile grid, matching the OME-Zarr and pyramidal-TIFF branches.
TILE_SIZE = 1024

#: Longest side of the source region one tile may read. A virtual level whose
#: nearest native level is `f` times bigger reads `TILE_SIZE * f` per side, so
#: this caps the in-flight downsample at 4x -- a 4096x4096 uint8 RGB region is
#: 50 MB, which is a slow tile but a tile. Past it, the levels are derived.
MAX_SOURCE_SIDE = 4096

#: Coarsest virtual level: one tile's worth. Same target `ome_zarr` derives to.
COARSEST_SIDE = TILE_SIZE

#: MRXS tiles that were never scanned come back fully transparent. Composited
#: onto white rather than left as zeros, which would draw them black -- the one
#: colour that means "stain" everywhere else in the image.
_BACKGROUND = 255

# Fluorophores and filter cubes. A channel called any of these is emitted
# light, whatever the file's storage layout says. Matched as whole words
# against a normalized name, so "DAPI" hits and "adapting" does not.
_FLUOROPHORE_WORDS = frozenset({
    "dapi", "hoechst", "draq5", "sytox", "propidium", "pi",
    "fitc", "tritc", "texas", "cy2", "cy3", "cy5", "cy7", "gfp", "yfp", "cfp",
    "rfp", "mcherry", "tdtomato", "egfp", "dsred", "phalloidin",
    "opal",  # Akoya's multiplex panels name their channels Opal 520, 570, ...
    "atto", "dylight", "alexa", "af", "cf",
})

#: Alexa Fluor / CF dye numbers ("AF488", "Alexa 647", "CF568").
_DYE_NUMBER = re.compile(r"\b(?:af|alexa(?:\s*fluor)?|cf|atto|dylight|opal)\s*[-_]?\s*\d{3}\b")

#: Names an RGB file's three samples are called when anyone bothers to name
#: them. Only meaningful as a complete set of three.
_RGB_NAME_SETS = (
    {"r", "g", "b"},
    {"red", "green", "blue"},
)

_PURE_RGB_COLORS = ({0xFF0000, 0x00FF00, 0x0000FF},
                    {0xFF0000FF, 0x00FF00FF, 0x0000FFFF})


class BrightfieldSupportMissing(RuntimeError):
    """A slide format that needs OpenSlide, in an environment without it.

    Carries the install line rather than the import error, because "No module
    named 'openslide'" in a web response tells the person holding the slide
    nothing they can act on.
    """


def _openslide():
    try:
        import openslide
    except ImportError as error:  # pragma: no cover - environment dependent
        raise BrightfieldSupportMissing(
            "Reading this slide format needs OpenSlide, which is not "
            "installed. Install it with:\n\n"
            "    pip install 'plexora[wsi]'\n\n"
            "If that reports that the OpenSlide library itself is missing, "
            "install the system package too -- `brew install openslide` on "
            "macOS, `apt install libopenslide0` on Debian/Ubuntu."
        ) from error
    return openslide


# -- verdicts ------------------------------------------------------------


@dataclass(frozen=True)
class Detection:
    """What `detect_image_type` concluded, and what it concluded it from.

    `reason` is shown to the user on the project edit page, next to the
    override that supersedes it -- so it is written as a sentence fragment
    somebody can check against their own knowledge of the file, not as a rule
    id. `confidence` is "high" when the file states its layout structurally,
    "medium" when metadata implies it, "low" when only the pixels did (or
    nothing did), and it is what decides whether the edit page volunteers the
    override or leaves it folded away.
    """

    verdict: str
    confidence: str
    reason: str

    @property
    def is_brightfield(self) -> bool:
        return self.verdict == BRIGHTFIELD


def _brightfield(confidence, reason) -> Detection:
    return Detection(BRIGHTFIELD, confidence, reason)


def _fluorescence(confidence, reason) -> Detection:
    return Detection(FLUORESCENCE, confidence, reason)


# -- detection -----------------------------------------------------------


def is_openslide_format(path) -> bool:
    return Path(path).suffix.lower() in OPENSLIDE_ONLY_SUFFIXES


def is_wsi_path(path) -> bool:
    """Whether `path` is a whole-slide container Plexora reads as an image.

    Extension only, and deliberately: this is what the import sniffer asks
    before it has decided to open anything.
    """
    return Path(path).suffix.lower() in WSI_SUFFIXES


def _normalize(name) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def _looks_fluorescent(names: Sequence[str]) -> Optional[str]:
    """The first channel name that names a fluorophore, or None."""
    for name in names:
        normalized = _normalize(name)
        if not normalized:
            continue
        if _DYE_NUMBER.search(normalized):
            return str(name)
        if any(word in _FLUOROPHORE_WORDS for word in normalized.split()):
            return str(name)
    return None


def _looks_like_rgb_names(names: Sequence[str]) -> bool:
    cleaned = {_normalize(name) for name in names}
    return len(names) == 3 and any(cleaned == expected for expected in _RGB_NAME_SETS)


def _tiff_layout(path) -> Optional[dict]:
    """Storage facts about a TIFF's first series, or None if it is not a TIFF.

    Everything Bio-Formats' `isRGB()` is derived from, plus the axis string
    tifffile computes -- an `S` in it is the sample dimension, which is the
    same statement as `SamplesPerPixel > 1` arrived at from the other side.
    """
    import tifffile as tf

    try:
        with tf.TiffFile(str(path), is_ome=False) as handle:
            page = handle.pages[0]
            series = handle.series[0]
            description = ""
            try:
                description = str(page.tags["ImageDescription"].value)
            except Exception:
                description = ""
            return {
                "photometric": int(getattr(page, "photometric", 0) or 0),
                "samples": int(getattr(page, "samplesperpixel", 1) or 1),
                "planar": int(getattr(page, "planarconfig", 1) or 1),
                "axes": str(getattr(series, "axes", "") or ""),
                "shape": tuple(int(d) for d in series.shape),
                "dtype": np.dtype(series.dtype),
                "description": description,
                "is_ome": bool(handle.is_ome),
                "kind": str(getattr(series, "kind", "") or ""),
            }
    except Exception:
        return None


# tifffile's PHOTOMETRIC enum values, spelled out so this module does not have
# to import the enum to compare against it.
_PHOTOMETRIC_RGB = 2
_PHOTOMETRIC_PALETTE = 3
_PHOTOMETRIC_YCBCR = 6


def _ome_channel_facts(description) -> Optional[dict]:
    """Per-channel OME-XML facts, or None when the description is not OME-XML.

    Read with a regex rather than `ome_types.from_xml`, on purpose: this runs
    during detection, before anything has decided the file is worth a full
    model parse, and a malformed or truncated OME block is exactly the case
    where the ladder should fall through to the next tier instead of raising.
    """
    if "<OME" not in (description or ""):
        return None
    channels = re.findall(r"<Channel\b[^>]*>", description)
    if not channels:
        return {"channels": 0, "samples": [], "contrast": [],
                "illumination": [], "names": [], "colors": []}

    def attribute(tag, key):
        found = re.search(rf'\b{key}="([^"]*)"', tag)
        return found.group(1) if found else None

    samples, contrast, illumination, names, colors = [], [], [], [], []
    for tag in channels:
        raw = attribute(tag, "SamplesPerPixel")
        samples.append(int(raw) if raw and raw.isdigit() else None)
        contrast.append((attribute(tag, "ContrastMethod") or "").lower() or None)
        illumination.append((attribute(tag, "IlluminationType") or "").lower() or None)
        names.append(attribute(tag, "Name"))
        raw_color = attribute(tag, "Color")
        try:
            colors.append(int(raw_color) & 0xFFFFFFFF if raw_color is not None else None)
        except ValueError:
            colors.append(None)
    return {"channels": len(channels), "samples": samples, "contrast": contrast,
            "illumination": illumination, "names": names, "colors": colors}


# OME `ContrastMethod` values that describe transmitted light, and the ones
# that describe emitted light. The enumeration is the most direct statement a
# file can make about which mode it was acquired in, and files that carry it
# are right about it -- so it outranks the storage layout, in both directions.
_BRIGHTFIELD_CONTRAST = {"brightfield", "phase", "dic", "hoffmanmodulation",
                         "obliqueillumination", "polarizedlight", "darkfield"}
_FLUORESCENCE_CONTRAST = {"fluorescence", "multiphotonfluorescence"}


def _detect_from_ome(facts) -> Optional[Detection]:
    if not facts or not facts["channels"]:
        return None

    contrast = [value for value in facts["contrast"] if value]
    if contrast:
        if all(value in _FLUORESCENCE_CONTRAST for value in contrast):
            return _fluorescence(
                "high", "the OME metadata declares ContrastMethod=Fluorescence")
        if all(value in _BRIGHTFIELD_CONTRAST for value in contrast):
            return _brightfield(
                "high",
                f"the OME metadata declares ContrastMethod={contrast[0]}")

    illumination = [value for value in facts["illumination"] if value]
    if illumination:
        if all(value == "epifluorescence" for value in illumination):
            return _fluorescence(
                "high", "the OME metadata declares IlluminationType=Epifluorescence")
        if all(value == "transmitted" for value in illumination):
            return _brightfield(
                "high", "the OME metadata declares IlluminationType=Transmitted")

    samples = [value for value in facts["samples"] if value]
    if facts["channels"] == 1 and samples == [3]:
        return _brightfield(
            "high",
            "the OME metadata describes one channel carrying three samples, "
            "which is how OME-XML spells RGB")
    if facts["channels"] >= 2 and samples and all(value == 1 for value in samples):
        return _fluorescence(
            "high",
            f"the OME metadata describes {facts['channels']} separate "
            "single-sample channels")
    return None


def _detect_from_names(names, colors=()) -> Optional[Detection]:
    fluorophore = _looks_fluorescent(names)
    if fluorophore:
        return _fluorescence(
            "medium", f"a channel is named {fluorophore!r}, which is a fluorophore")
    if _looks_like_rgb_names(names):
        return _brightfield(
            "medium", "the three channels are named red, green and blue")
    values = {int(value) for value in colors if value is not None}
    if values and any(values == expected for expected in _PURE_RGB_COLORS):
        return _brightfield(
            "medium",
            "the three channels are coloured pure red, green and blue")
    return None


def _thumbnail(path, layout, longest: int = 512) -> Optional[np.ndarray]:
    """A small (y, x, c) or (y, x) sample of the image, or None.

    Taken from the coarsest pyramid level so it costs one small read whatever
    the slide's full resolution is, and strided down from there. A file with no
    pyramid is read with a stride rather than in full, which is why this can be
    called on a 40000x40000 single-level TIFF without thinking about it.
    """
    import tifffile as tf
    import zarr

    try:
        with tf.TiffFile(str(path), is_ome=False) as handle:
            series = handle.series[0]
            level = series.levels[-1] if series.levels else series
            array = zarr.open(level.aszarr(), mode="r")
            if not hasattr(array, "shape"):
                array = array[sorted(array.array_keys(), key=int)[-1]]
            shape = array.shape
            axes = str(getattr(level, "axes", "") or "")
            rows, cols = (0, 1) if axes.endswith("S") else (len(shape) - 2, len(shape) - 1)
            step = max(1, int(max(shape[rows], shape[cols]) // longest))
            if axes.endswith("S"):
                return np.asarray(array[::step, ::step, ...])
            if len(shape) == 2:
                return np.asarray(array[::step, ::step])
            return np.moveaxis(np.asarray(array[:, ::step, ::step]), 0, -1)
    except Exception:
        return None


#: How much of a thumbnail has to be near-white before "it has a background"
#: is a fair thing to say. Uniformly distributed 8-bit noise puts about 13% of
#: its pixels over 220 by arithmetic alone, and noise is neither kind of image.
_LIGHT_FRACTION = 0.2

#: How alike the three planes have to be. Stain density modulates all three
#: samples of a transmitted-light image together, so they track each other
#: closely; independent fluorophores in independent channels do not. Set low
#: because the job here is to exclude the uncorrelated case, not to grade the
#: correlated one.
_CHANNEL_CORRELATION = 0.5


def _detect_from_pixels(path, layout) -> Optional[Detection]:
    """QuPath's move: look at the picture.

    Transmitted light images are bright almost everywhere -- the slide is a
    lamp with tissue in front of it -- and fluorescence images are dark almost
    everywhere, because the background is the absence of signal. Counting the
    two tails of the histogram separates them far more reliably than the mean
    does, since a densely packed H&E section and an empty fluorescence field
    can share a mean.

    Two conditions rather than QuPath's one, because QuPath only reaches its
    version *after* deciding the file is RGB, and this is reached for anything
    with three planes. Both extra conditions exclude the same thing from
    different sides -- an image whose planes are not one picture. A brightfield
    slide has a real white background (not merely more light pixels than dark
    ones, which uniform noise also has), and its three samples move together,
    because what varies across it is how much light the stain absorbed.

    Only reached when the file has said nothing structural, so a lean here is
    reported as low confidence: it is the tier the override exists for.
    """
    dtype = layout.get("dtype") if layout else None
    if dtype is not None and np.dtype(dtype).itemsize > 1:
        return _fluorescence(
            "low",
            "the pixels are more than 8 bits deep, which a colour camera's "
            "output is not")

    sample = _thumbnail(path, layout)
    if sample is None or sample.size == 0:
        return None
    if sample.ndim == 3:
        grey = sample[..., :3].mean(axis=-1)
    else:
        grey = sample.astype(np.float32)

    dark = float(np.count_nonzero(grey < 25))
    light = float(np.count_nonzero(grey > 220))
    if dark >= light or light < _LIGHT_FRACTION * grey.size:
        return _fluorescence(
            "low",
            "the image has no light background, the way an image of emitted "
            "light does not")

    if sample.ndim == 3 and sample.shape[-1] >= 3 and \
            _channel_correlation(sample) < _CHANNEL_CORRELATION:
        return _fluorescence(
            "low",
            "the image is light, but its three planes do not track each other "
            "the way the samples of one colour image do")

    return _brightfield(
        "low",
        "the image is mostly light, the way an image taken through a slide is")


def _channel_correlation(sample: np.ndarray) -> float:
    """The weakest pairwise Pearson correlation among the first three planes.

    The weakest rather than the mean: two planes agreeing says nothing if the
    third is unrelated to both, which is exactly what a panel with one crowded
    marker and two sparse ones looks like.
    """
    flat = sample[..., :3].reshape(-1, 3).astype(np.float64)
    if flat.shape[0] < 2:
        return 1.0
    if np.ptp(flat, axis=0).min() == 0:
        # A constant plane has no correlation to measure. Treated as agreement
        # rather than as disagreement: a blank sample is not evidence.
        return 1.0
    with np.errstate(invalid="ignore"):
        matrix = np.corrcoef(flat, rowvar=False)
    if not np.all(np.isfinite(matrix)):
        return 1.0
    return float(min(matrix[0, 1], matrix[0, 2], matrix[1, 2]))


def detect_image_type(path) -> Detection:
    """Whether the image at `path` is brightfield or fluorescence.

    Structural evidence first, pixels last, and never the channel count on its
    own -- a 3-plex fluorescence panel is a real thing and calling it H&E would
    be the worst mistake available here. See the module docstring for the
    ladder; the returned `reason` names whichever rung answered.
    """
    path = Path(path)

    if is_wsi_path(path):
        return _brightfield(
            "high",
            f"{path.suffix.lower()} is a whole-slide format that only holds "
            "brightfield images")

    from plexora.server.utils import ome_zarr

    if ome_zarr.is_zarr_image_path(path):
        return _detect_zarr(path)

    layout = _tiff_layout(path)
    if layout is None:
        return _fluorescence(
            "low", "the file could not be inspected, so the default was kept")

    facts = _ome_channel_facts(layout["description"])

    # The OME block, when there is one, outranks the storage layout: a file
    # that states ContrastMethod is telling us something the byte layout
    # cannot, and a planar-RGB brightfield image exists.
    ome = _detect_from_ome(facts)
    if ome is not None and ome.confidence == "high":
        return ome

    if is_rgb_layout(path):
        return _brightfield(
            "high",
            "the pixels are stored as interleaved RGB samples in one plane, "
            "which is how a colour camera writes and a channel stack does not")

    if ome is not None:
        return ome

    names = _detect_from_names(facts["names"] if facts else [],
                               facts["colors"] if facts else ())
    if names is not None:
        return names

    pixels = _detect_from_pixels(path, layout)
    if pixels is not None:
        return _with_enough_planes(pixels, layout)

    return _fluorescence(
        "low", "nothing in the file says it is brightfield, so it is read as "
               "a channel stack")


def plane_count(layout) -> int:
    """How many planes a TIFF layout can supply, interleaved or not."""
    if not layout:
        return 0
    axes = layout["axes"]
    if axes.endswith("S"):
        return int(layout["samples"])
    if "C" in axes:
        return int(layout["shape"][axes.index("C")])
    return 1 if len(layout["shape"]) == 2 else int(layout["shape"][0])


def _with_enough_planes(verdict: Detection, layout) -> Detection:
    """`verdict`, unless it says brightfield about something with no colour.

    Brightfield means three samples read together. A one- or two-plane image
    can still be *bright* -- a scanned grayscale IHC section is -- and the
    pixel tier would happily say so, which would then be read as RGB and fail
    on the missing planes. One plane is a channel stack of one, which is a
    thing Plexora already draws.
    """
    if verdict.verdict != BRIGHTFIELD or plane_count(layout) >= 3:
        return verdict
    return _fluorescence(
        "low",
        "the image is light, but it has fewer than three samples, so there is "
        "no colour to read")


def _detect_zarr(path) -> Detection:
    """The NGFF branch of the ladder.

    Nothing in OME-Zarr states acquisition mode, so the only evidence a store
    carries is what `omero.channels` was labelled and coloured with -- and a
    store with no `omero` block at all says nothing, which is fluorescence by
    default the same as everywhere else.
    """
    from plexora.server.utils import ome_zarr

    try:
        pyramid = ome_zarr.open_image(path)
        count = int(pyramid[0].shape[0])
        labels = ome_zarr.channel_labels(path, count) or []
    except Exception:
        return _fluorescence(
            "low", "the store could not be inspected, so the default was kept")

    colors = []
    try:
        attrs = ome_zarr._ome_attrs(ome_zarr._open_group(path))
        for entry in (attrs.get("omero") or {}).get("channels", []) or []:
            raw = entry.get("color") if isinstance(entry, dict) else None
            colors.append(int(str(raw), 16) if raw else None)
    except Exception:
        colors = []

    verdict = _detect_from_names(labels, colors)
    if verdict is not None:
        return verdict
    return _fluorescence(
        "low", "the store's metadata says nothing about acquisition mode, so "
               "it is read as a channel stack")


def is_rgb_layout(path) -> bool:
    """Whether the pixels at `path` declare themselves to be colour.

    A statement about *storage*, not about mode: an RGB file read as
    fluorescence still comes through here, because the CYX view is the only
    thing that can turn interleaved samples into three planes. Every reader in
    Plexora that would otherwise index a TIFF as `shape[0] == channels` asks
    this first -- without it, an interleaved slide records its own height as
    its channel count and its width as 3.

    Bio-Formats' `isRGB()` plus one restriction it does not make: the samples
    have to be *interleaved*. That restriction is not about what can be read --
    `open_rgb` reads separate planes perfectly well -- it is about what
    `photometric=RGB` is worth as evidence. tifffile stores any three-plane
    uint8 array as separate-component RGB **by default**, with no photometric
    argument given, so a large fraction of the 8-bit fluorescence stacks in
    existence declare themselves colour without anybody having meant it.
    Interleaved samples are not written by accident: they are how a colour
    camera writes and how a channel stack never does.

    So what this does NOT cover is a file whose three planes are separate,
    whatever its photometric says. Those are read as colour only when something
    else agrees -- the detector's later tiers, or the user's override -- which
    is what the `rgb=True` argument on the local provider carries.
    """
    path = Path(path)
    if is_wsi_path(path):
        return True
    layout = _tiff_layout(path)
    if layout is None:
        return False
    interleaved = layout["axes"].endswith("S") or layout["planar"] == 1
    return (layout["photometric"] in (_PHOTOMETRIC_RGB, _PHOTOMETRIC_YCBCR)
            and layout["samples"] >= 3 and interleaved)


# -- native sources ------------------------------------------------------


class _InterleavedSource:
    """A native level stored (y, x, samples) -- how a colour TIFF writes."""

    __slots__ = ("_array", "height", "width")

    def __init__(self, array):
        self._array = array
        self.height = int(array.shape[0])
        self.width = int(array.shape[1])

    def read(self, y0, y1, x0, x1) -> np.ndarray:
        return np.asarray(self._array[y0:y1, x0:x1, :3])


class _PlanarSource:
    """A native level stored (samples, y, x) -- planar RGB, and the derived
    levels `build_extension` writes, which are CYX by construction."""

    __slots__ = ("_array", "height", "width")

    def __init__(self, array):
        self._array = array
        self.height = int(array.shape[-2])
        self.width = int(array.shape[-1])

    def read(self, y0, y1, x0, x1) -> np.ndarray:
        block = np.asarray(self._array[:3, y0:y1, x0:x1])
        return np.ascontiguousarray(np.moveaxis(block, 0, -1))


class _OpenSlideSource:
    """One OpenSlide level.

    `read_region` takes its origin in *level 0* coordinates whatever level it
    is reading, which is the one detail that makes this wrapper worth having:
    getting it wrong reads the right-sized rectangle from the wrong place, and
    the image looks fine until you compare it with anything.
    """

    __slots__ = ("_slide", "_level", "_downsample", "height", "width")

    def __init__(self, slide, level: int):
        self._slide = slide
        self._level = int(level)
        self._downsample = float(slide.level_downsamples[level])
        self.width, self.height = (int(value) for value in slide.level_dimensions[level])

    def read(self, y0, y1, x0, x1) -> np.ndarray:
        width, height = max(0, x1 - x0), max(0, y1 - y0)
        if not width or not height:
            return np.zeros((height, width, 3), dtype=np.uint8)
        origin = (int(round(x0 * self._downsample)),
                  int(round(y0 * self._downsample)))
        region = np.asarray(self._slide.read_region(origin, self._level,
                                                    (width, height)))
        # RGBA -> RGB over white. A sparse slide's unscanned tiles arrive fully
        # transparent, and dropping alpha rather than compositing would paint
        # them black -- the colour that means "stain" everywhere else.
        if region.shape[-1] == 4:
            alpha = region[..., 3:4].astype(np.uint16)
            rgb = region[..., :3].astype(np.uint16)
            blended = (rgb * alpha + _BACKGROUND * (255 - alpha)) // 255
            return blended.astype(np.uint8)
        return np.ascontiguousarray(region[..., :3])


# -- levels --------------------------------------------------------------


class _RgbAccessor:
    """`level.rgb[rows, cols]` -- the one place colour leaves this module."""

    __slots__ = ("_level",)

    def __init__(self, level):
        self._level = level

    def __getitem__(self, index):
        if not isinstance(index, tuple):
            index = (index,)
        rows = index[0] if len(index) > 0 else slice(None)
        cols = index[1] if len(index) > 1 else slice(None)
        return self._level.read_rgb(rows, cols)


class _Level:
    """One level of the halving chain, read from the nearest native level.

    Presents `(3, height, width)` and indexes `[channel, rows, cols]` like
    every other pyramid level in Plexora, so quantization, overview building,
    `build_extension` and the node's region reads need no knowledge of colour.
    `.rgb[rows, cols]` is the additional accessor the tile path uses.

    `height`/`width` are the *dyadic* size for this level index, which is not
    always the source's own size: when the nearest native level is off by a
    pixel or a factor, the read is resampled to the size the viewer asked for
    rather than the size the scanner happened to write.
    """

    __slots__ = ("_source", "shape", "ndim", "dtype", "chunks", "scale_y", "scale_x")

    def __init__(self, source, height: int, width: int):
        self._source = source
        self.shape = (3, int(height), int(width))
        self.ndim = 3
        self.dtype = np.dtype(np.uint8)
        self.chunks = (1, TILE_SIZE, TILE_SIZE)
        self.scale_y = source.height / float(height)
        self.scale_x = source.width / float(width)

    @property
    def rgb(self) -> _RgbAccessor:
        return _RgbAccessor(self)

    @staticmethod
    def _bounds(index, extent):
        """`index` as a clipped (start, stop) inside `extent`.

        Slicing past the end is normal here and not an error: the viewer's tile
        grid is `ceil(size / 1024)` wide, so the last tile of every row and
        column asks for pixels that are not there and gets a short one back,
        exactly as a numpy or zarr slice would give it.
        """
        span = index if isinstance(index, slice) \
            else slice(int(index), int(index) + 1)
        start, stop, _ = span.indices(int(extent))
        return start, max(start, stop)

    def read_rgb(self, rows, cols) -> np.ndarray:
        """`(h, w, 3)` uint8 for a rectangle of this level."""
        y0, y1 = self._bounds(rows, self.shape[1])
        x0, x1 = self._bounds(cols, self.shape[2])
        out_h, out_w = y1 - y0, x1 - x0
        if out_h <= 0 or out_w <= 0:
            return np.zeros((max(out_h, 0), max(out_w, 0), 3), dtype=np.uint8)

        source = self._source
        sy0 = min(source.height, int(math.floor(y0 * self.scale_y)))
        sy1 = min(source.height, max(sy0 + 1, int(math.ceil(y1 * self.scale_y))))
        sx0 = min(source.width, int(math.floor(x0 * self.scale_x)))
        sx1 = min(source.width, max(sx0 + 1, int(math.ceil(x1 * self.scale_x))))
        block = source.read(sy0, sy1, sx0, sx1)
        if block.shape[0] == out_h and block.shape[1] == out_w:
            return block
        return _resize(block, out_h, out_w)

    def __getitem__(self, index):
        if not isinstance(index, tuple):
            index = (index,)
        channel = index[0]
        if not isinstance(channel, (int, np.integer)):
            raise TypeError(
                "a brightfield level is indexed as [channel, rows, cols] with "
                f"an integer channel, not {channel!r}")
        rows = index[1] if len(index) > 1 else slice(None)
        cols = index[2] if len(index) > 2 else slice(None)
        return self.read_rgb(rows, cols)[..., int(channel)]

    def __array__(self, dtype=None, copy=None):
        block = self.read_rgb(slice(None), slice(None))
        stack = np.ascontiguousarray(np.moveaxis(block, -1, 0))
        return stack.astype(dtype) if dtype is not None else stack


def _resize(block: np.ndarray, height: int, width: int) -> np.ndarray:
    """`block` resampled to (height, width, 3), area-averaged.

    PIL's BOX filter rather than skimage: this is on the tile path, the input
    is always uint8 RGB, and BOX is exactly the box average `_reduce2` does for
    the derived levels -- so a tile served through here and the same tile
    served from a derived level differ by rounding, not by method.
    """
    from PIL import Image

    if block.size == 0:
        return np.zeros((height, width, 3), dtype=np.uint8)
    resample = Image.BOX if (block.shape[0] >= height and block.shape[1] >= width) \
        else Image.BILINEAR
    image = Image.fromarray(np.ascontiguousarray(block), mode="RGB")
    return np.asarray(image.resize((width, height), resample))


class RgbPyramid:
    """A brightfield image's resolution levels, shaped like the zarr group
    tifffile produces for a pyramidal TIFF.

    Deliberately not a `zarr.Array` and deliberately without `.shape`, for the
    same reason `ome_zarr.NgffPyramid` is not: both are how existing code tells
    a pyramid from a single plane (`data_model._zarr_level`, `read_tile`'s
    isinstance branches, `node/api.py`'s `hasattr(pyramid, "shape")`).
    """

    def __init__(self, levels, *, path=None, extension=None,
                 base_levels: Optional[int] = None, handle=None,
                 detection: Optional[Detection] = None):
        self._levels = list(levels)
        self.path = str(path) if path is not None else None
        self.extension = str(extension) if extension is not None else None
        #: How many levels can be served straight from the file. The rest were
        #: derived; see `open_rgb`.
        self.base_levels = len(self._levels) if base_levels is None else base_levels
        #: The open file/slide handle, held so it outlives this object's use.
        self._handle = handle
        self.detection = detection

    def __len__(self) -> int:
        return len(self._levels)

    def __iter__(self):
        return iter(str(index) for index in range(len(self._levels)))

    def __contains__(self, key) -> bool:
        try:
            index = int(key)
        except (TypeError, ValueError):
            return False
        return 0 <= index < len(self._levels)

    def __getitem__(self, key):
        try:
            index = int(key)
        except (TypeError, ValueError):
            raise KeyError(key) from None
        if not 0 <= index < len(self._levels):
            raise KeyError(key)
        return self._levels[index]

    @property
    def level_shapes(self) -> list[list[int]]:
        return [[int(level.shape[-2]), int(level.shape[-1])] for level in self._levels]


# -- opening -------------------------------------------------------------


def _native_sources(path):
    """(sources, handle) for `path`, finest first.

    `handle` is returned rather than closed because every source above reads
    lazily from it -- closing it here would give back a pyramid whose every
    tile raises.
    """
    if is_openslide_format(path):
        slide = _openslide().OpenSlide(str(path))
        return [_OpenSlideSource(slide, index)
                for index in range(slide.level_count)], slide

    import tifffile as tf
    import zarr

    handle = tf.TiffFile(str(path), is_ome=False)
    series = handle.series[0]
    interleaved = str(getattr(series, "axes", "") or "").endswith("S")
    store = zarr.open(series.aszarr(), mode="r")
    if hasattr(store, "shape"):
        arrays = [store]
    else:
        arrays = [store[key] for key in sorted(store.array_keys(), key=int)]
    make = _InterleavedSource if interleaved else _PlanarSource
    return [make(array) for array in arrays], handle


def _dyadic_shapes(height: int, width: int, target: int = COARSEST_SIDE):
    """The halving chain from (height, width) down to one tile.

    `ceil` at every step, matching `ome_zarr.build_extension` -- so a derived
    level and a virtual level of the same index agree on their size, which is
    what lets the two be appended to one list and indexed by the same number.
    """
    shapes = [[int(height), int(width)]]
    while max(shapes[-1]) > target:
        shapes.append([-(-shapes[-1][0] // 2), -(-shapes[-1][1] // 2)])
    return shapes


def _pick_source(sources, height: int, width: int):
    """The cheapest native level that can produce a (height, width) view.

    The smallest one that is still at least as big, so the read is a
    downsample rather than a blur -- with a pixel of slack, because a scanner
    that rounded an odd dimension down wrote a level that is one short of the
    halving and is still obviously the right one to use.
    """
    best = sources[0]
    for source in sources:
        if source.height + 1 >= height and source.width + 1 >= width:
            best = source
    return best


def _affordable(source, height: int, width: int) -> bool:
    """Whether one tile of a (height, width) level is a bounded read.

    The source rectangle behind a tile is `TILE_SIZE * source_size / level_size`
    per side, clipped to the source itself -- so a coarse level of a *small*
    source stays affordable however large the ratio, which is what keeps the
    last two levels of a 4x slide pyramid on the virtual path instead of
    triggering a conversion nobody needed.
    """
    scale_y = source.height / float(height)
    scale_x = source.width / float(width)
    return (min(TILE_SIZE * scale_y, source.height) <= MAX_SOURCE_SIDE
            and min(TILE_SIZE * scale_x, source.width) <= MAX_SOURCE_SIDE)


def open_rgb(path, extension=None) -> RgbPyramid:
    """The brightfield image at `path` as an `RgbPyramid`, finest level first.

    The levels are the halving chain the viewer's tile source assumes, not the
    ones the file happens to contain: each is read from the nearest native
    level and resampled in flight. The chain stops at the first level whose
    nearest native source is too far away to read a tile from (see
    `_affordable`) -- for a slide with any pyramid at all that is never, and
    for one written flat it is the point where `build_extension`'s derived
    levels take over, appended here from `extension`.
    """
    sources, handle = _native_sources(path)
    finest = sources[0]
    shapes = _dyadic_shapes(finest.height, finest.width)

    levels = []
    for height, width in shapes:
        source = _pick_source(sources, height, width)
        if levels and not _affordable(source, height, width):
            break
        levels.append(_Level(source, height, width))

    base_levels = len(levels)
    if extension and Path(extension).exists():
        import zarr

        derived = zarr.open_group(str(extension), mode="r")
        index = base_levels
        while str(index) in derived:
            array = derived[str(index)]
            levels.append(_Level(_PlanarSource(array),
                                 int(array.shape[-2]), int(array.shape[-1])))
            index += 1

    return RgbPyramid(levels, path=path, extension=extension,
                      base_levels=base_levels, handle=handle)


def rgb_region(level, rows, cols) -> np.ndarray:
    """`(h, w, 3)` uint8 from a pyramid level, whichever kind of level it is.

    `_Level.rgb` is the fast path and the one every ordinary brightfield read
    takes. The fallback exists for the one arrangement that reaches a
    brightfield tile request through a plain zarr level: an image whose three
    planes are `minisblack` -- so nothing in the file says it is colour --
    served by a **data node**, which has no project to have been told
    otherwise. Stacking three planes there is exactly what the level view would
    have done, without needing the node to have heard about the decision.
    """
    accessor = getattr(level, "rgb", None)
    if accessor is not None:
        return np.asarray(accessor[rows, cols])
    planes = [np.asarray(level[channel, rows, cols]) for channel in range(3)]
    stacked = np.stack(planes, axis=-1)
    if stacked.dtype != np.uint8:
        # A 16-bit source read as colour: scaled by its own dtype range rather
        # than windowed, because a brightfield image has no window and the
        # alternative -- clipping at 255 -- would be a white rectangle.
        info = np.iinfo(stacked.dtype) if np.issubdtype(stacked.dtype, np.integer) else None
        ceiling = float(info.max) if info is not None else float(stacked.max() or 1)
        stacked = np.clip(stacked / ceiling * 255.0, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(stacked)


def needs_extension(pyramid) -> bool:
    """Whether the levels that can be served reach far enough out.

    False for any slide whose own pyramid covers the zoom range, which is the
    normal case and the reason importing a 300 MB SVS writes nothing.

    Asked as "did the chain reach one tile", not as a size threshold like the
    NGFF path's. The two are different questions because the levels here are
    virtual: an NGFF store that stops early serves `maxLevel = 1` and the
    viewer only ever asks for a plain slice of level 0, while a truncated
    virtual chain still ADVERTISES its coarse levels -- and every tile of them
    would resample a rectangle the size of the whole image. So the point at
    which `open_rgb` gave up is exactly the point where derived levels are
    worth writing.
    """
    height, width = pyramid.level_shapes[0]
    return len(pyramid) < len(_dyadic_shapes(height, width))


def extension_path(data_directory) -> Path:
    """Where a brightfield image's derived levels live.

    A different name from the NGFF one: a project has exactly one image, so
    they could not collide, but a directory holding `brightfield_pyramid.zarr`
    says what it is without opening it.
    """
    return Path(data_directory) / "brightfield_pyramid.zarr"


def build_extension(pyramid, dest, progress_callback=None) -> Optional[str]:
    """Derive the coarse levels `pyramid` cannot serve, into a zarr store.

    Delegates to `ome_zarr.build_extension` unchanged: the CYX level views
    above are already the input contract it wants (`.shape` as (c, y, x),
    `[channel, rows]` slicing, a `.dtype`), and it names its output arrays by
    absolute level index, which `open_rgb` appends by the same number.
    """
    from plexora.server.utils import ome_zarr

    return ome_zarr.build_extension(pyramid, dest,
                                    progress_callback=progress_callback)


# -- what the rest of the server asks for --------------------------------


def geometry(pyramid) -> dict:
    """Shape facts, in `local.image_geometry`'s vocabulary.

    `num_channels` is 3 -- the number of planes the pyramid really has, which
    is what a node's geometry check compares against and what the fluorescence
    override serves. How many *layers the viewer draws* is a different number
    and lives in `imageData`, not here.
    """
    finest = pyramid[0]
    return {
        "levels": len(pyramid),
        "num_channels": int(finest.shape[0]),
        "height": int(finest.shape[-2]),
        "width": int(finest.shape[-1]),
        "tile_height": TILE_SIZE,
        "tile_width": TILE_SIZE,
        "level_shapes": pyramid.level_shapes,
    }


def overview_plane(pyramid, minimum: int = 200, maximum: int = 400) -> np.ndarray:
    """A materialized coarse level as (3, y, x), for the mini-map and stats.

    Same heuristic as the TIFF and NGFF paths -- the smallest level with both
    dimensions >= `minimum`, pooled down when it is still well above it --
    which is what makes it bounded whatever the slide's full resolution is.
    """
    from skimage.measure import block_reduce

    candidates = [index for index in range(len(pyramid))
                  if all(d >= minimum for d in pyramid[index].shape[-2:])]
    index = candidates[-1] if candidates else 0
    array = np.asarray(pyramid[index])
    if array.shape[-2] > maximum or array.shape[-1] > maximum:
        factor = int(min(array.shape[-2] // minimum, array.shape[-1] // minimum))
        if factor > 1:
            array = block_reduce(array, (1, factor, factor), np.mean).astype(np.uint8)
    return array


#: `|MPP = 0.2465|` in an Aperio ImageDescription: microns per pixel, and the
#: only place an SVS states its scale.
_APERIO_MPP = re.compile(r"\|\s*MPP\s*=\s*([0-9.]+)", re.IGNORECASE)

#: `<pixelSize>` / `sizeX` in the Leica XML an SCN carries.
_LEICA_SIZE = re.compile(r"<sizeX>\s*([0-9.]+)\s*</sizeX>", re.IGNORECASE)

# TIFF ResolutionUnit -> how many micrometres one unit is.
_RESOLUTION_UNIT_UM = {2: 25400.0, 3: 10000.0}


def _ratio(value) -> Optional[float]:
    try:
        if isinstance(value, tuple) and len(value) == 2 and value[1]:
            return float(value[0]) / float(value[1])
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def physical_metadata(path) -> dict:
    """`{physical_size_x, physical_size_x_unit, ...}` for the viewer's scale bar.

    The same two keys `/get_ome_metadata` already serves for an OME-TIFF, so
    the scale bar needs no knowledge of where the numbers came from. Each
    format states its scale somewhere different and none of them states it
    twice, so this is four small readers and an empty dict when none of them
    finds anything -- which is the state the scale bar already hides itself for.
    """
    path = Path(path)

    if is_openslide_format(path):
        try:
            slide = _openslide().OpenSlide(str(path))
            x = _ratio(slide.properties.get("openslide.mpp-x"))
            y = _ratio(slide.properties.get("openslide.mpp-y")) or x
            slide.close()
            if x:
                return {"physical_size_x": x, "physical_size_x_unit": "µm",
                        "physical_size_y": y, "physical_size_y_unit": "µm"}
        except Exception:
            pass
        return {}

    import tifffile as tf

    try:
        with tf.TiffFile(str(path), is_ome=False) as handle:
            page = handle.pages[0]
            description = ""
            try:
                description = str(page.tags["ImageDescription"].value)
            except Exception:
                description = ""

            aperio = _APERIO_MPP.search(description)
            if aperio:
                size = float(aperio.group(1))
                return {"physical_size_x": size, "physical_size_x_unit": "µm",
                        "physical_size_y": size, "physical_size_y_unit": "µm"}

            if "<OME" in description:
                found = _ome_pixel_size(description)
                if found:
                    return found

            leica = _LEICA_SIZE.search(description)
            if leica and float(leica.group(1)):
                size = float(leica.group(1))
                return {"physical_size_x": size, "physical_size_x_unit": "µm",
                        "physical_size_y": size, "physical_size_y_unit": "µm"}

            unit = int(getattr(page.tags.get("ResolutionUnit"), "value", 0) or 0)
            scale = _RESOLUTION_UNIT_UM.get(unit)
            x = _ratio(getattr(page.tags.get("XResolution"), "value", None))
            y = _ratio(getattr(page.tags.get("YResolution"), "value", None)) or x
            if scale and x:
                return {"physical_size_x": scale / x, "physical_size_x_unit": "µm",
                        "physical_size_y": scale / (y or x),
                        "physical_size_y_unit": "µm"}
    except Exception:
        pass
    return {}


_OME_PIXEL_SIZE = re.compile(
    r'PhysicalSize([XY])="([0-9.eE+-]+)"(?:[^>]*?PhysicalSize\1Unit="([^"]*)")?')


def _ome_pixel_size(description) -> dict:
    out: dict[str, Any] = {}
    for axis, value, unit in _OME_PIXEL_SIZE.findall(description):
        try:
            size = float(value)
        except ValueError:
            continue
        if not size:
            continue
        key = f"physical_size_{axis.lower()}"
        out[key] = size
        out[f"{key}_unit"] = unit or "µm"
    return out


__all__ = [
    "BRIGHTFIELD",
    "BRIGHTFIELD_ONLY_SUFFIXES",
    "BrightfieldSupportMissing",
    "Detection",
    "FLUORESCENCE",
    "RGB_CHANNEL_KEY",
    "RgbPyramid",
    "WSI_SUFFIXES",
    "build_extension",
    "detect_image_type",
    "extension_path",
    "geometry",
    "is_openslide_format",
    "is_rgb_layout",
    "is_wsi_path",
    "needs_extension",
    "open_rgb",
    "overview_plane",
    "physical_metadata",
    "plane_count",
    "rgb_region",
]
