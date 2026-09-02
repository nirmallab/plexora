"""Reading DICOM Whole Slide Microscopy images, whatever shape they arrived in.

Every other image format Plexora reads is one file (or one store) that holds
one picture. DICOM is not: a single slide is a *collection* of instances, and
the collection is described only by the metadata inside its members. One
40000x24000 multiplex slide can be 252 separate `.dcm` files -- 36 optical
paths at 7 resolution levels -- sitting in a directory whose name says nothing,
alongside the label and overview photographs of the same glass. So the first
job here is not decoding pixels, it is *reconstruction*: read the headers, work
out which files are one slide, and which axis each file varies along.

That reconstruction is `wsidicom`'s to do, and this module does not attempt it
by hand. What lives here is everything on either side of it:

**Which files are one slide?** `assemble_slide` groups instances by their
Container Identifier (the barcode on the glass), falling back to the Frame of
Reference and then the Series UID, always inside one Study. Point it at a
directory and it finds the slide; point it at one `.dcm` and it gathers that
file's siblings. A directory holding *two* slides is an error naming both,
because guessing which one somebody meant is the kind of help nobody wants.
The result is a `SlideSource`, which is also the seam DICOMweb arrives at
later: `kind="files"` today, `kind="web"` when a QIDO/WADO provider exists,
and nothing downstream of `SlideSource.open()` knows the difference.

**What do its dimensions mean?** A DICOM slide varies along four axes and only
one of them is a biological channel. Optical paths ARE the markers -- a t-CyCIF
slide names them `DNA`, `CD45`, `Vimentin` in Optical Path Description, and
those become the channel names. Focal planes are Z and are pinned to the middle
plane here (recorded, never flattened into channels: showing somebody's z-stack
as 3 extra markers would be a lie about their experiment). Resolution levels
are the pyramid. Frames are tiles. And the label/overview/thumbnail instances
are photographs of the slide and its barcode -- excluded from the pyramid
entirely, because they are not the specimen.

**Which reading is it?** Brightfield DICOM states `PhotometricInterpretation`
= RGB/YBR and reads as one colour image through the exact `.rgb` seam
`brightfield.py` built; multiplex fluorescence states MONOCHROME2 and reads as
N grayscale planes. DICOM says which it is in its own header, so unlike a TIFF
this needs no thumbnail heuristics -- `detect_image_type` is metadata only, and
the user's override exists for the files whose metadata lies.

**Levels are virtual**, for the same reason as in `brightfield.py`: the viewer's
tile source assumes a halving chain, and a scanner's pyramid is whatever it is.
Each virtual level reads from the nearest native one and resamples in flight.
The levels a slide cannot afford to serve that way are derived once by
`ome_zarr.build_extension`, which the CYX level views below already satisfy.

The pyramid this returns honours the same duck contract as `RgbPyramid` and
`NgffPyramid` -- `pyramid[str(level)]`, no `.shape`, levels indexed
`[channel, rows, cols]` -- which is what lets DICOM arrive without a second
viewer: tile encoding, quantization, the mini-map, node serving and the Figure
Builder never learn that this one came out of a PACS.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from plexora.server.utils import brightfield

#: The SOP Class every tiled microscopy instance Plexora reads declares. Used
#: to tell the slide's own instances from anything else that shares a folder
#: with them -- a structured report, a radiology series, a stray DICOMDIR.
WSI_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.77.1.6"

#: What a file has to be called to be worth opening. DICOM does not require an
#: extension at all, but every WSI exporter in practice writes one, and the
#: alternative -- sniffing the "DICM" preamble of every file in a directory --
#: turns picking a folder into a full scan of whatever else is in it.
DICOM_SUFFIXES = (".dcm", ".dicom")

#: The index into `ImageType` (0008,0008) that says what a WSI instance is a
#: picture *of*: VOLUME is the specimen, LABEL is the barcode, OVERVIEW is the
#: whole glass, THUMBNAIL is a preview. Only VOLUME is the image.
_FLAVOR_INDEX = 2
VOLUME = "VOLUME"

#: Photometric interpretations that mean "these samples are one colour", and
#: the one that means "this plane is one channel's intensity".
_COLOR_PHOTOMETRICS = ("RGB", "YBR_FULL", "YBR_FULL_422", "YBR_ICT", "YBR_RCT",
                       "YBR_PARTIAL_420", "YBR_PARTIAL_422")
_MONOCHROME = "MONOCHROME2"

#: The virtual tile grid, and the two limits that decide where the virtual
#: levels stop. Taken from `brightfield` rather than restated: a derived level
#: and a virtual level of the same index have to agree on their size, and the
#: cheapest way to guarantee that is to share the arithmetic.
TILE_SIZE = brightfield.TILE_SIZE
MAX_SOURCE_SIDE = brightfield.MAX_SOURCE_SIDE
COARSEST_SIDE = brightfield.COARSEST_SIDE

#: `image_kind` for a DICOM slide read as fluorescence. A new kind rather than
#: `ome_tiff` because the path is not a TIFF and `_missing_pyramid` has to be
#: able to tell them apart; the client never compares against it (every
#: `image_kind` test in the browser is `== "brightfield"` or `== "rgb"`), so it
#: behaves exactly like `ome_tiff` there. A DICOM slide read as *brightfield*
#: records `brightfield` instead -- same reason: nothing about drawing it
#: differs, so nothing should have to know.
IMAGE_KIND = "dicom"

#: How deep a picked directory is searched for instances. Real exports nest --
#: the HTAN layout is `<slide>/<study uid>/<series uid>/*.dcm` -- but a
#: directory tree is not a search space, and stopping at four levels keeps
#: "is this a DICOM folder?" a bounded question for a home directory too.
_MAX_SCAN_DEPTH = 4

#: Directory suffixes that are somebody else's format. A `.zarr` store is a
#: directory of files, and walking into one looking for `.dcm` is wasted work.
_FOREIGN_DIRECTORY_SUFFIXES = (".zarr", ".n5")


class DicomSupportMissing(RuntimeError):
    """A DICOM slide, in an environment without the libraries that read one.

    Carries the install line rather than the ImportError, for the same reason
    `BrightfieldSupportMissing` does: "No module named 'wsidicom'" in a web
    response tells the person holding the slide nothing they can act on.
    """


def _wsidicom():
    try:
        import wsidicom
    except ImportError as error:  # pragma: no cover - environment dependent
        raise DicomSupportMissing(
            "Reading DICOM whole-slide images needs wsidicom, which is not "
            "installed. Install it with:\n\n"
            "    pip install 'plexora[wsi]'"
        ) from error
    return wsidicom


def _pydicom():
    try:
        import pydicom
    except ImportError as error:  # pragma: no cover - environment dependent
        raise DicomSupportMissing(
            "Reading DICOM whole-slide images needs pydicom, which is not "
            "installed. Install it with:\n\n"
            "    pip install 'plexora[wsi]'"
        ) from error
    return pydicom


# -- what counts as DICOM ------------------------------------------------


def _is_dicom_file(path: Path) -> bool:
    return (path.suffix.lower() in DICOM_SUFFIXES
            or path.name.upper() == "DICOMDIR")


def _first_dicom_file(directory: Path, max_depth: int = _MAX_SCAN_DEPTH) -> Optional[Path]:
    """The first `.dcm` at or under `directory`, breadth-first, or None.

    Breadth-first and bounded, because this answers "is this a DICOM folder?"
    for anything a user can point a file picker at. The common layouts put
    instances either directly in the picked folder or two levels down under
    study and series UIDs, so the answer arrives after a handful of `scandir`
    calls; a folder that is not one gives up rather than walking a disk.
    """
    frontier = [(directory, 0)]
    while frontier:
        current, depth = frontier.pop(0)
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        children = []
        for entry in entries:
            try:
                if entry.is_file():
                    candidate = Path(entry.path)
                    if _is_dicom_file(candidate):
                        return candidate
                elif entry.is_dir() and depth < max_depth:
                    if not entry.name.lower().endswith(_FOREIGN_DIRECTORY_SUFFIXES):
                        children.append((Path(entry.path), depth + 1))
            except OSError:
                continue
        frontier.extend(children)
    return None


def is_dicom_path(path) -> bool:
    """Whether `path` is a DICOM slide, or a directory holding one.

    Names and directory structure only -- nothing here opens an instance. This
    is what the import sniffer and the provider dispatch ask before they have
    decided to read anything, so it has to be answerable for a path that turns
    out to be a `.zarr` store or somebody's Downloads folder.
    """
    if not path:
        return False
    candidate = Path(path)
    if candidate.is_dir():
        if candidate.name.lower().endswith(_FOREIGN_DIRECTORY_SUFFIXES):
            return False
        return _first_dicom_file(candidate) is not None
    return _is_dicom_file(candidate)


# -- reading the headers -------------------------------------------------


#: Everything grouping, detection and naming need out of an instance header.
#: Listed so `dcmread` can be told to stop there: a WSI header carries the
#: entire specimen preparation record -- antibody clones, dilutions, vendor
#: catalogue numbers -- and 252 complete parses is a visible pause where 252
#: partial ones are not. The Optical Path Sequence is in the list because
#: reading it here is what makes naming a slide's channels free: a multiplex
#: export puts one path in each of its files, so the marker names only exist
#: once every file has been looked at, and looking twice would double the wait.
_SCAN_TAGS = [
    "SOPClassUID", "StudyInstanceUID", "SeriesInstanceUID",
    "ContainerIdentifier", "FrameOfReferenceUID", "ImageType",
    "PhotometricInterpretation", "SamplesPerPixel", "BitsAllocated",
    "TotalPixelMatrixColumns", "TotalPixelMatrixRows",
    "TotalPixelMatrixFocalPlanes", "NumberOfOpticalPaths",
    "OpticalPathSequence",
]


@dataclass(frozen=True)
class _Instance:
    """One `.dcm` file, as much of it as grouping and detection need."""

    path: Path
    study_uid: str
    series_uid: str
    container: str
    frame_of_reference: str
    flavor: str
    photometric: str
    samples: int
    bits: int
    columns: int
    rows: int
    focal_planes: int
    #: `(identifier, description, wavelength)` per optical path in this file.
    optical_paths: tuple[tuple[str, Optional[str], Optional[float]], ...] = ()

    @property
    def is_volume(self) -> bool:
        return self.flavor == VOLUME

    @property
    def is_color(self) -> bool:
        return self.photometric in _COLOR_PHOTOMETRICS or self.samples >= 3


def _read_instance(path: Path) -> Optional[_Instance]:
    """`path` as an `_Instance`, or None if it is not a WSI instance.

    Every failure mode lands on None on purpose: a directory chosen by a human
    contains whatever it contains, and one unreadable file is not a reason to
    refuse the slide it is sitting next to.
    """
    pydicom = _pydicom()
    try:
        dataset = pydicom.dcmread(str(path), stop_before_pixels=True,
                                  specific_tags=_SCAN_TAGS)
    except Exception:
        return None
    if str(getattr(dataset, "SOPClassUID", "")) != WSI_SOP_CLASS_UID:
        return None

    image_type = [str(value) for value in (getattr(dataset, "ImageType", None) or [])]
    flavor = image_type[_FLAVOR_INDEX] if len(image_type) > _FLAVOR_INDEX else VOLUME

    def text(name) -> str:
        return str(getattr(dataset, name, "") or "").strip()

    def number(name, default=0) -> int:
        try:
            return int(getattr(dataset, name, default) or default)
        except (TypeError, ValueError):
            return default

    optical_paths = []
    for entry in getattr(dataset, "OpticalPathSequence", None) or []:
        identifier = str(getattr(entry, "OpticalPathIdentifier", "") or "").strip()
        description = str(getattr(entry, "OpticalPathDescription", "") or "").strip()
        try:
            wavelength = float(getattr(entry, "IlluminationWaveLength", None))
        except (TypeError, ValueError):
            wavelength = None
        optical_paths.append((identifier, description or None, wavelength))

    return _Instance(
        path=path,
        study_uid=text("StudyInstanceUID"),
        series_uid=text("SeriesInstanceUID"),
        container=text("ContainerIdentifier"),
        frame_of_reference=text("FrameOfReferenceUID"),
        flavor=flavor,
        photometric=text("PhotometricInterpretation").upper(),
        samples=number("SamplesPerPixel", 1),
        bits=number("BitsAllocated", 8),
        columns=number("TotalPixelMatrixColumns"),
        rows=number("TotalPixelMatrixRows"),
        focal_planes=number("TotalPixelMatrixFocalPlanes", 1),
        optical_paths=tuple(optical_paths),
    )


def _scan(paths: Sequence[Path]) -> list[_Instance]:
    found = []
    for path in paths:
        instance = _read_instance(path)
        if instance is not None:
            found.append(instance)
    return found


def _candidate_files(directory: Path, max_depth: int = _MAX_SCAN_DEPTH) -> list[Path]:
    """Every `.dcm` at or under `directory`, to `max_depth`."""
    files: list[Path] = []
    frontier = [(directory, 0)]
    while frontier:
        current, depth = frontier.pop(0)
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file():
                    candidate = Path(entry.path)
                    if candidate.suffix.lower() in DICOM_SUFFIXES:
                        files.append(candidate)
                elif entry.is_dir() and depth < max_depth:
                    if not entry.name.lower().endswith(_FOREIGN_DIRECTORY_SUFFIXES):
                        frontier.append((Path(entry.path), depth + 1))
            except OSError:
                continue
    return files


def _group_key(instance: _Instance) -> tuple[str, str]:
    """What makes two instances the same slide.

    The Container Identifier is the barcode printed on the glass, which is the
    only identifier in DICOM that means "this physical slide" -- it holds
    across the several series a multiplex scan is often split into, and it
    differs between two slides that were scanned in one session and therefore
    share a study. Where an exporter left it out, the Frame of Reference is the
    next best thing (one coordinate system is one piece of glass), and the
    Series UID is the floor: it is always present, and it never merges two
    slides, it only ever splits one.
    """
    return (instance.study_uid,
            instance.container or instance.frame_of_reference or instance.series_uid)


def _group_label(instances: Sequence[_Instance]) -> str:
    first = instances[0]
    return first.container or first.frame_of_reference or first.series_uid or "(unidentified)"


# -- where a slide's instances live --------------------------------------


@dataclass(frozen=True)
class SlideSource:
    """The instances of one logical slide, and how to open them.

    The seam DICOM's two access routes meet at. `kind="files"` is a resolved
    list of paths on a filesystem -- which, because Plexora runs a data node
    next to the data rather than copying the data to Plexora, already covers a
    gcsfuse-mounted bucket and a share on the scanner's host. `kind="web"` is
    the DICOMweb follow-up: a QIDO-discovered study/series opened through
    `WsiDicom.open_web` against a WADO-RS endpoint, with frame retrieval taking
    the place of file reads.

    Both produce the same `WsiDicom` handle, so `open()` is the only place in
    Plexora that would have to learn the difference.
    """

    kind: str
    files: tuple[Path, ...] = ()
    label: str = ""

    def open(self):
        if self.kind != "files":
            raise ValueError(
                f"{self.kind!r} DICOM sources are not supported yet; this "
                "build reads slides from files.")
        if not self.files:
            raise ValueError("No DICOM instances to open.")
        # An explicit file list rather than the containing folder: wsidicom's
        # folder mode globs one level, and real exports nest their instances
        # under study and series UIDs, so handing it the folder a user picked
        # finds nothing at all.
        return _wsidicom().WsiDicom.open([str(path) for path in self.files])


def _describe_groups(groups) -> str:
    lines = []
    for key, instances in groups:
        volumes = [instance for instance in instances if instance.is_volume]
        widest = max((instance.columns for instance in volumes), default=0)
        tallest = max((instance.rows for instance in volumes), default=0)
        size = f", {widest}x{tallest}" if widest and tallest else ""
        lines.append(f"  {_group_label(instances)} "
                     f"({len(instances)} files{size})")
    return "\n".join(lines)


def _assemble(path) -> tuple[SlideSource, list[_Instance]]:
    """The slide at `path`, and the headers it was worked out from.

    Both, from one scan. Splitting them into two entry points would be tidier
    to read and would double the cost of every open: assembly, detection and
    naming all want the same 252 headers, and reading them four times is the
    difference between a project that registers in two seconds and one that
    registers in eight.
    """
    picked = Path(path)
    if picked.name.upper() == "DICOMDIR":
        picked = picked.parent

    if picked.is_dir():
        instances = _scan(_candidate_files(picked))
        if not instances:
            raise ValueError(
                f"{picked.name} holds no DICOM whole-slide images. Plexora "
                "opens a folder of DICOM when it contains VL Whole Slide "
                "Microscopy instances.")
        groups: dict[tuple[str, str], list[_Instance]] = {}
        for instance in instances:
            groups.setdefault(_group_key(instance), []).append(instance)
        if len(groups) > 1:
            ordered = sorted(groups.items(), key=lambda item: _group_label(item[1]))
            raise ValueError(
                f"{picked.name} holds {len(groups)} slides, and Plexora opens "
                "one image at a time. Pick a .dcm file from the slide you "
                f"want:\n{_describe_groups(ordered)}")
        chosen = next(iter(groups.values()))
    else:
        seed = _read_instance(picked)
        if seed is None:
            raise ValueError(
                f"{picked.name} is not a DICOM whole-slide image.")
        # Siblings only. The instances of one slide are written together, and
        # widening the search to the whole tree would pull in the neighbouring
        # slide of a two-slide export -- which is the case the directory branch
        # above refuses to guess about, so it must not be guessed here either.
        key = _group_key(seed)
        chosen = [instance for instance in _scan(_candidate_files(picked.parent, 0))
                  if _group_key(instance) == key]
        if not chosen:  # pragma: no cover - the seed always matches itself
            chosen = [seed]

    ordered = sorted(chosen, key=lambda instance: instance.path)
    source = SlideSource(kind="files",
                         files=tuple(instance.path for instance in ordered),
                         label=_group_label(ordered))
    return source, ordered


def assemble_slide(path) -> SlideSource:
    """The logical slide `path` names, gathered from its metadata.

    Three ways in, one answer. A **directory** is scanned and grouped; one
    slide opens, several is an error that names them, because a folder of
    slides is a folder of slides and Plexora shows one image. A **single
    `.dcm`** is always unambiguous -- it selects its own slide -- so its
    siblings are gathered around it, which is what pulls in the other 251 files
    of a 36-marker pyramid from one click. A **DICOMDIR** is read as the folder
    that contains it.
    """
    return _assemble(path)[0]


def slide_instances(path) -> list[_Instance]:
    """The scanned headers of the slide at `path`. Detection's input."""
    return _assemble(path)[1]


# -- detection -----------------------------------------------------------


def _volume_instances(instances: Sequence[_Instance]) -> list[_Instance]:
    volumes = [instance for instance in instances if instance.is_volume]
    return volumes or list(instances)


def _sorted_identifiers(identifiers: Sequence[str]) -> list[str]:
    """Optical path identifiers in a stable, human order.

    Necessary rather than tidy: wsidicom reports a level's paths in the order
    it happened to read the instances, and that order differs between levels of
    the same slide. Channel 3 has to be the same marker at every zoom, so the
    order is imposed here -- numerically when the identifiers are numbers,
    which is how every exporter in practice writes acquisition order.
    """
    unique = sorted(set(identifiers))
    if all(value.isdigit() for value in unique if value):
        return sorted(unique, key=lambda value: (not value.isdigit(),
                                                 int(value) if value.isdigit() else 0))
    return unique


def _preview(names: Sequence[str], limit: int = 3) -> str:
    shown = [name for name in names if name][:limit]
    if not shown:
        return ""
    suffix = ", ..." if len(names) > len(shown) else ""
    return f" ({', '.join(shown)}{suffix})"


def detect_image_type(path) -> brightfield.Detection:
    """Whether the DICOM slide at `path` is brightfield or fluorescence.

    Metadata only, and confident about it. Every other format in Plexora needs
    a ladder of guesses ending in a look at the pixels, because a TIFF can be
    written by anything; a WSI DICOM instance is required to state its
    Photometric Interpretation, and RGB versus MONOCHROME2 is exactly the
    distinction being drawn. So there is no thumbnail tier here and no low
    confidence -- when the header is readable it is believed, and when it is
    not, the override is the answer rather than a coin flip.
    """
    try:
        instances = _volume_instances(slide_instances(path))
    except DicomSupportMissing:
        raise
    except Exception:
        return brightfield.Detection(
            brightfield.FLUORESCENCE, "low",
            "the DICOM headers could not be read, so the default was kept")
    return _detect(instances)


def _detect(instances: Sequence[_Instance]) -> brightfield.Detection:
    if not instances:  # pragma: no cover - assemble_slide raises first
        return brightfield.Detection(
            brightfield.FLUORESCENCE, "low",
            "the slide holds no image instances, so the default was kept")

    first = instances[0]
    if first.is_color:
        return brightfield.Detection(
            brightfield.BRIGHTFIELD, "high",
            f"the DICOM instances store {first.photometric or 'colour'} samples, "
            "which is how a brightfield scanner writes and a channel stack "
            "does not")

    identifiers = _sorted_identifiers(
        [identifier for instance in instances
         for identifier, _, _ in instance.optical_paths if identifier])
    names = _names_for(instances, identifiers) or []
    focal = max(instance.focal_planes for instance in instances)
    depth = f", {focal} focal planes" if focal > 1 else ""
    photometric = first.photometric or "monochrome"

    if len(identifiers) <= 1:
        return brightfield.Detection(
            brightfield.FLUORESCENCE, "high",
            f"the DICOM instances are {photometric} with a single optical "
            f"path{_preview(names)}{depth}")
    return brightfield.Detection(
        brightfield.FLUORESCENCE, "high",
        f"the DICOM instances are {photometric} with {len(identifiers)} "
        f"optical paths{_preview(names)}{depth}")


# There is deliberately no `is_rgb_layout` here to match `brightfield`'s.
# That function exists because a TIFF's dispatch has to decide which reader to
# use BEFORE opening anything, and interleaved samples cannot be indexed as
# planes by the wrong one. DICOM has no such split: one reader opens every
# slide and `DicomPyramid.is_color` is the answer, read from the headers it had
# to parse anyway. A second, path-based way to ask the same question would be a
# second place for it to be answered differently.


# -- channel names -------------------------------------------------------


#: Concept codes a specimen preparation step uses to say what was stained for.
#: `Component investigated` is the marker itself; the fluorophore codes name
#: the dye, which is a worse channel name but a real one.
_COMPONENT_CODES = ("246094008",)          # SCT, "Component investigated"
_CHANNEL_CODES = ("C44170",)               # NCIt, "Channel"


def _staining_names(path: Path) -> dict[str, str]:
    """`{optical path identifier: marker}` from the specimen preparation record.

    The second tier of the name ladder. A multiplex exporter that fills in the
    full staining history writes one step per cycle, each naming the channel it
    produced and the component it investigated -- so this recovers marker names
    for a slide whose Optical Path Descriptions were left blank, which is the
    shape a lot of converted data has.
    """
    pydicom = _pydicom()
    try:
        dataset = pydicom.dcmread(str(path), stop_before_pixels=True,
                                  specific_tags=["SpecimenDescriptionSequence"])
    except Exception:
        return {}

    names: dict[str, str] = {}
    for description in getattr(dataset, "SpecimenDescriptionSequence", None) or []:
        for step in getattr(description, "SpecimenPreparationSequence", None) or []:
            channel, marker = None, None
            for item in getattr(step, "SpecimenPreparationStepContentItemSequence",
                                None) or []:
                concepts = getattr(item, "ConceptNameCodeSequence", None) or []
                code = str(getattr(concepts[0], "CodeValue", "")) if concepts else ""
                text = str(getattr(item, "TextValue", "") or "").strip()
                if code in _CHANNEL_CODES and text:
                    channel = text
                elif code in _COMPONENT_CODES:
                    values = getattr(item, "ConceptCodeSequence", None) or []
                    marker = text or (str(getattr(values[0], "CodeMeaning", "") or "").strip()
                                      if values else "")
            if channel and marker:
                names.setdefault(channel, marker)
    return names


def _names_for(instances: Sequence[_Instance],
               identifiers: Sequence[str]) -> Optional[list[str]]:
    """Display names for `identifiers`, in channel order, or None.

    Three tiers and a floor, all-or-nothing: a panel where only some markers
    can be named is worse than one named `Channel 1..N`, because a half-labelled
    layer list reads as though the unlabelled ones are missing rather than
    merely unnamed. Description first -- it is where a multiplex exporter puts
    the marker, and a t-CyCIF slide names all 36 of them there -- then the
    staining record, then the illumination wavelength, which at least tells the
    channels apart.
    """
    if not identifiers:
        return None

    descriptions: dict[str, str] = {}
    wavelengths: dict[str, float] = {}
    for instance in instances:
        for identifier, description, wavelength in instance.optical_paths:
            if not identifier:
                continue
            if description:
                descriptions.setdefault(identifier, description)
            if wavelength:
                wavelengths.setdefault(identifier, wavelength)

    # The staining record is one read of one file, and only worth making when
    # the descriptions did not already answer for every channel.
    staining: dict[str, str] = {}
    if any(identifier not in descriptions for identifier in identifiers):
        staining = _staining_names(instances[0].path)

    names = []
    for identifier in identifiers:
        name = descriptions.get(identifier) or staining.get(identifier)
        if not name and identifier in wavelengths:
            name = f"{wavelengths[identifier]:g} nm"
        if not name:
            return None
        names.append(str(name))
    return names


def channel_names(path) -> Optional[list[str]]:
    """The slide's marker names, or None to fall back to "Channel N".

    `datasource._channel_names_from_image_metadata`'s DICOM tier. Never raises:
    names are a nicety and tiles are not, so a slide whose optical path
    metadata is malformed still opens, just anonymously.
    """
    try:
        instances = _volume_instances(slide_instances(path))
        if not instances or instances[0].is_color:
            return None
        identifiers = _sorted_identifiers(
            [identifier for instance in instances
             for identifier, _, _ in instance.optical_paths if identifier])
        return _names_for(instances, identifiers)
    except Exception:
        return None


# -- native sources ------------------------------------------------------


def _to_plane(region, dtype) -> np.ndarray:
    """A `read_region` result as a 2-D array of `dtype`.

    wsidicom hands back a PIL image, and for 16-bit monochrome that is mode
    `I` -- 32-bit signed integers, because PIL has no unsigned 16-bit mode.
    Casting back is not cosmetic: everything downstream windows uint16 into
    uint8 with a 65536-entry lookup table, and an int32 plane would index it
    out of range.
    """
    array = np.asarray(region)
    if array.ndim == 3:
        array = array[..., 0]
    return array.astype(dtype, copy=False)


class _MonoSource:
    """One (native level, optical path) of an open slide, as a grayscale plane.

    Reads are addressed in the *requested level's* own coordinates, which is
    the opposite of OpenSlide's convention and the one detail worth a class of
    its own: passing level-0 coordinates reads the right-sized rectangle from
    the wrong place, and at a coarse level that is the whole slide away.
    """

    __slots__ = ("_wsi", "_level", "_path_id", "_z", "_dtype", "height", "width")

    def __init__(self, wsi, level: int, size, path_id: str, z, dtype):
        self._wsi = wsi
        self._level = int(level)
        self._path_id = str(path_id)
        self._z = z
        self._dtype = np.dtype(dtype)
        self.width, self.height = int(size[0]), int(size[1])

    def read(self, y0, y1, x0, x1) -> np.ndarray:
        # Clipped here as well as by the level above, because wsidicom raises
        # on a region that leaves the level rather than returning a short one,
        # and the tile grid's last row and column always ask for one.
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(self.width, int(x1)), min(self.height, int(y1))
        width, height = max(0, x1 - x0), max(0, y1 - y0)
        if not width or not height:
            return np.zeros((height, width), dtype=self._dtype)
        region = self._wsi.read_region((x0, y0), self._level, (width, height),
                                       path=self._path_id, z=self._z)
        return _to_plane(region, self._dtype)


class _ColorSource:
    """One native level of a brightfield slide, as interleaved RGB."""

    __slots__ = ("_wsi", "_level", "_path_id", "_z", "height", "width")

    def __init__(self, wsi, level: int, size, path_id, z):
        self._wsi = wsi
        self._level = int(level)
        self._path_id = str(path_id) if path_id is not None else None
        self._z = z
        self.width, self.height = int(size[0]), int(size[1])

    def read(self, y0, y1, x0, x1) -> np.ndarray:
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(self.width, int(x1)), min(self.height, int(y1))
        width, height = max(0, x1 - x0), max(0, y1 - y0)
        if not width or not height:
            return np.zeros((height, width, 3), dtype=np.uint8)
        region = self._wsi.read_region((x0, y0), self._level, (width, height),
                                       path=self._path_id, z=self._z)
        array = np.asarray(region)
        if array.ndim == 2:
            array = np.repeat(array[..., None], 3, axis=-1)
        if array.dtype != np.uint8:
            array = array.astype(np.uint8, copy=False)
        return np.ascontiguousarray(array[..., :3])


# -- levels --------------------------------------------------------------


def _bounds(index, extent):
    """`index` as a clipped (start, stop) inside `extent`.

    Slicing past the end is normal and not an error: the viewer's tile grid is
    `ceil(size / 1024)` wide, so the last tile of every row and column asks for
    pixels that are not there and gets a short one back, exactly as a numpy or
    zarr slice would give it.
    """
    span = index if isinstance(index, slice) else slice(int(index), int(index) + 1)
    start, stop, _ = span.indices(int(extent))
    return start, max(start, stop)


def _resize_plane(block: np.ndarray, height: int, width: int) -> np.ndarray:
    """`block` resampled to (height, width), area-averaged, dtype preserved.

    Through PIL's 32-bit float mode rather than the integer ones: a fluorescence
    plane is uint16, which PIL cannot resize directly, and going via float keeps
    the box average an average instead of rounding every intermediate.
    """
    from PIL import Image

    if block.size == 0:
        return np.zeros((height, width), dtype=block.dtype)
    resample = Image.BOX if (block.shape[0] >= height and block.shape[1] >= width) \
        else Image.BILINEAR
    image = Image.fromarray(np.ascontiguousarray(block, dtype=np.float32), mode="F")
    resized = np.asarray(image.resize((width, height), resample))
    if np.issubdtype(block.dtype, np.integer):
        info = np.iinfo(block.dtype)
        return np.clip(np.rint(resized), info.min, info.max).astype(block.dtype)
    return resized.astype(block.dtype)


class _MonoLevel:
    """One level of a fluorescence slide: `(paths, height, width)`.

    Indexed `[channel, rows, cols]` like every other pyramid level in Plexora,
    where "channel" is an optical path -- so quantization, the overview, the
    node's region reads and `build_extension` need no knowledge of DICOM at
    all. Each channel reads independently, which is what makes toggling one
    marker in the viewer cost one marker's tiles.

    `height`/`width` are the *dyadic* size for this level index, which is not
    always the source's own: where the nearest native level is off by a pixel
    or a factor, the read is resampled to the size the viewer asked for.
    """

    __slots__ = ("_sources", "shape", "ndim", "dtype", "chunks", "scale_y", "scale_x")

    def __init__(self, sources, height: int, width: int, dtype):
        self._sources = list(sources)
        self.shape = (len(self._sources), int(height), int(width))
        self.ndim = 3
        self.dtype = np.dtype(dtype)
        self.chunks = (1, TILE_SIZE, TILE_SIZE)
        first = self._sources[0]
        self.scale_y = first.height / float(height)
        self.scale_x = first.width / float(width)

    def read_plane(self, channel: int, rows, cols) -> np.ndarray:
        y0, y1 = _bounds(rows, self.shape[1])
        x0, x1 = _bounds(cols, self.shape[2])
        out_h, out_w = y1 - y0, x1 - x0
        if out_h <= 0 or out_w <= 0:
            return np.zeros((max(out_h, 0), max(out_w, 0)), dtype=self.dtype)

        source = self._sources[int(channel)]
        sy0 = min(source.height, int(math.floor(y0 * self.scale_y)))
        sy1 = min(source.height, max(sy0 + 1, int(math.ceil(y1 * self.scale_y))))
        sx0 = min(source.width, int(math.floor(x0 * self.scale_x)))
        sx1 = min(source.width, max(sx0 + 1, int(math.ceil(x1 * self.scale_x))))
        block = source.read(sy0, sy1, sx0, sx1)
        if block.shape[0] == out_h and block.shape[1] == out_w:
            return block
        return _resize_plane(block, out_h, out_w)

    def __getitem__(self, index):
        if not isinstance(index, tuple):
            index = (index,)
        channel = index[0]
        if not isinstance(channel, (int, np.integer)):
            raise TypeError(
                "a DICOM level is indexed as [channel, rows, cols] with an "
                f"integer channel, not {channel!r}")
        rows = index[1] if len(index) > 1 else slice(None)
        cols = index[2] if len(index) > 2 else slice(None)
        return self.read_plane(int(channel), rows, cols)

    def __array__(self, dtype=None, copy=None):
        planes = [self.read_plane(channel, slice(None), slice(None))
                  for channel in range(self.shape[0])]
        stack = np.stack(planes, axis=0)
        return stack.astype(dtype) if dtype is not None else stack


class _ColorLevel:
    """One level of a brightfield slide: `(3, height, width)` uint8.

    The same two faces `brightfield._Level` has. `.rgb[rows, cols]` is the
    colour seam the tile route reads, and `[channel, rows, cols]` is the same
    pixels presented as three planes -- which is what makes a "Fluorescence"
    override of an H&E slide an honest reading rather than a special case.
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
    def rgb(self):
        return brightfield._RgbAccessor(self)

    def read_rgb(self, rows, cols) -> np.ndarray:
        y0, y1 = _bounds(rows, self.shape[1])
        x0, x1 = _bounds(cols, self.shape[2])
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
        return brightfield._resize(block, out_h, out_w)

    def __getitem__(self, index):
        if not isinstance(index, tuple):
            index = (index,)
        channel = index[0]
        if not isinstance(channel, (int, np.integer)):
            raise TypeError(
                "a DICOM colour level is indexed as [channel, rows, cols] with "
                f"an integer channel, not {channel!r}")
        rows = index[1] if len(index) > 1 else slice(None)
        cols = index[2] if len(index) > 2 else slice(None)
        return self.read_rgb(rows, cols)[..., int(channel)]

    def __array__(self, dtype=None, copy=None):
        block = self.read_rgb(slice(None), slice(None))
        stack = np.ascontiguousarray(np.moveaxis(block, -1, 0))
        return stack.astype(dtype) if dtype is not None else stack


class _ZarrSource:
    """A derived level out of the extension store, as (c, y, x)."""

    __slots__ = ("_array", "_channel", "height", "width")

    def __init__(self, array, channel: int):
        self._array = array
        self._channel = int(channel)
        self.height = int(array.shape[-2])
        self.width = int(array.shape[-1])

    def read(self, y0, y1, x0, x1) -> np.ndarray:
        return np.asarray(self._array[self._channel, y0:y1, x0:x1])


class DicomPyramid:
    """A DICOM slide's resolution levels, shaped like every other pyramid here.

    Deliberately not a `zarr.Array` and deliberately without `.shape`, for the
    same reason `RgbPyramid` and `NgffPyramid` are not: both are how existing
    code tells a pyramid from a single plane (`data_model._zarr_level`,
    `read_tile`'s isinstance branches, `node/api.py`'s `hasattr(pyramid,
    "shape")`).

    The DICOM-specific facts hang off it rather than off the levels, because
    they are properties of the slide: which optical paths it has and what they
    are called, how many focal planes it has and which one is being shown,
    whether it carries a label or overview photograph, and the pixel spacing.
    """

    def __init__(self, levels, *, path=None, extension=None,
                 base_levels: Optional[int] = None, handle=None,
                 source: Optional[SlideSource] = None,
                 optical_paths: Sequence[str] = (),
                 channel_names: Optional[Sequence[str]] = None,
                 focal_plane: Optional[float] = None, focal_plane_count: int = 1,
                 pyramid_count: int = 1, has_label: bool = False,
                 has_overview: bool = False, is_color: bool = False,
                 mpp: Optional[tuple[float, float]] = None):
        self._levels = list(levels)
        self.path = str(path) if path is not None else None
        self.extension = str(extension) if extension is not None else None
        #: How many levels can be served straight from the slide. The rest were
        #: derived; see `open_image`.
        self.base_levels = len(self._levels) if base_levels is None else base_levels
        #: The open `WsiDicom` handle, held so it outlives this object's use --
        #: every level above reads lazily through it.
        self._handle = handle
        #: How the instances were reached. `kind="files"` today; the record is
        #: what a DICOMweb source would differ in and nothing else would.
        self.source = source
        self.optical_paths = tuple(str(value) for value in optical_paths)
        self.channel_names = list(channel_names) if channel_names else None
        #: Which focal plane the levels read, and how many the slide has. Z is
        #: pinned rather than exposed, and recorded rather than dropped: a
        #: z-stack is not a channel stack, and saying so is the point.
        self.focal_plane = focal_plane
        self.focal_plane_count = int(focal_plane_count)
        self.pyramid_count = int(pyramid_count)
        self.has_label = bool(has_label)
        self.has_overview = bool(has_overview)
        self.is_color = bool(is_color)
        self.mpp = mpp

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

    def close(self):
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


# -- opening -------------------------------------------------------------


def _focal_plane_of(wsi) -> tuple[Optional[float], int]:
    """The plane to read, and how many there are.

    The middle one, so a slide focused about its centre shows the tissue rather
    than the coverslip. Passed explicitly to every read afterwards rather than
    left to the default, so the plane a tile came from is a fact about the
    pyramid and not about which instance wsidicom happened to reach first.
    """
    try:
        planes = sorted(float(value) for value in wsi.levels[0].focal_planes)
    except Exception:
        return None, 1
    if not planes:
        return None, 1
    return planes[len(planes) // 2], len(planes)


def _native_levels(wsi):
    """`[(index, (width, height), {paths})]` for the slide's own levels, finest first."""
    levels = []
    for level in wsi.levels:
        size = (int(level.size.width), int(level.size.height))
        paths = {str(value) for value in (level.optical_paths or [])}
        levels.append((int(level.level), size, paths))
    levels.sort(key=lambda item: -item[1][0])
    return levels


class _NativeShape:
    """`_pick_source`/`_affordable` want an object with `.height` and `.width`."""

    __slots__ = ("height", "width", "index", "paths")

    def __init__(self, index, size, paths):
        self.index = index
        self.width, self.height = int(size[0]), int(size[1])
        self.paths = paths


def _dtype_for(instances: Sequence[_Instance]) -> np.dtype:
    bits = max((instance.bits for instance in instances), default=8)
    return np.dtype(np.uint16 if bits > 8 else np.uint8)


def open_image(path, extension=None, rgb: bool = False) -> DicomPyramid:
    """The DICOM slide at `path` as a `DicomPyramid`, finest level first.

    The levels are the halving chain the viewer's tile source assumes, not the
    ones the slide happens to contain: each reads from the nearest native level
    and resamples in flight. For a slide written with a real pyramid -- which
    is nearly all of them, since DICOM WSI exists to be tiled -- the two are the
    same sizes and nothing is resampled at all.

    `rgb` is the project's "read this as colour" override, and it is only
    honoured for a slide that HAS three samples to read. A monochrome multiplex
    slide cannot be shown as one colour image no matter who asks: there is no
    colour in it, and the three planes it would borrow are three markers.
    """
    source, scanned = _assemble(path)
    instances = _volume_instances(scanned)
    # `rgb` is accepted for signature parity with the other readers, and a
    # DICOM slide overrules it in one direction only: the header states how
    # many samples a pixel has, and no override can invent a third one. So a
    # colour slide is read as colour whether or not it was asked for (the
    # planes cannot be indexed otherwise), and a monochrome multiplex slide
    # stays monochrome however the project was configured.
    color = bool(instances) and instances[0].is_color
    dtype = _dtype_for(instances)

    wsi = source.open()
    try:
        focal_plane, focal_count = _focal_plane_of(wsi)
        natives = [_NativeShape(index, size, paths)
                   for index, size, paths in _native_levels(wsi)]
        finest = natives[0]

        if color:
            path_id = next(iter(sorted(finest.paths)), None)
            identifiers: list[str] = []
            names = None
        else:
            # Keyed on what wsidicom will accept as a `path=` argument, named
            # from what the headers said about those same identifiers -- the
            # scan above already read them, so naming the panel costs nothing.
            identifiers = _sorted_identifiers(sorted(finest.paths))
            names = _names_for(instances, identifiers)
            path_id = None

        # Only the native levels that carry every channel are usable sources: a
        # level missing an optical path could not answer for that channel, and
        # silently serving black would be worse than reading a finer level.
        usable = [native for native in natives
                  if color or set(identifiers) <= native.paths] or [finest]

        shapes = brightfield._dyadic_shapes(finest.height, finest.width)
        levels: list[Any] = []
        for height, width in shapes:
            native = brightfield._pick_source(usable, height, width)
            if levels and not brightfield._affordable(native, height, width):
                break
            if color:
                levels.append(_ColorLevel(
                    _ColorSource(wsi, native.index, (native.width, native.height),
                                 path_id, focal_plane), height, width))
            else:
                sources = [_MonoSource(wsi, native.index,
                                       (native.width, native.height),
                                       identifier, focal_plane, dtype)
                           for identifier in identifiers]
                levels.append(_MonoLevel(sources, height, width, dtype))

        base_levels = len(levels)
        if extension and Path(extension).exists():
            import zarr

            derived = zarr.open_group(str(extension), mode="r")
            index = base_levels
            while str(index) in derived:
                array = derived[str(index)]
                height = int(array.shape[-2])
                width = int(array.shape[-1])
                if color:
                    levels.append(_ColorLevel(
                        brightfield._PlanarSource(array), height, width))
                else:
                    levels.append(_MonoLevel(
                        [_ZarrSource(array, channel)
                         for channel in range(int(array.shape[0]))],
                        height, width, dtype))
                index += 1

        return DicomPyramid(
            levels, path=path, extension=extension, base_levels=base_levels,
            handle=wsi, source=source, optical_paths=identifiers,
            channel_names=names, focal_plane=focal_plane,
            focal_plane_count=focal_count,
            pyramid_count=len(getattr(wsi, "pyramids", None) or [1]),
            has_label=bool(getattr(wsi, "labels", None)),
            has_overview=bool(getattr(wsi, "overviews", None)),
            is_color=color, mpp=_mpp_of(wsi))
    except Exception:
        try:
            wsi.close()
        except Exception:
            pass
        raise


def _mpp_of(wsi) -> Optional[tuple[float, float]]:
    """Micrometres per pixel at the finest level, or None.

    wsidicom's `mpp` is named for the millimetre type it is carried in but
    holds micrometres, which is also the unit the viewer's scale bar wants --
    so this is a read, not a conversion.
    """
    try:
        mpp = wsi.levels[0].mpp
        return float(mpp.width), float(mpp.height)
    except Exception:
        return None


# -- what the rest of the server asks for --------------------------------


def geometry(pyramid) -> dict:
    """Shape facts, in `local.image_geometry`'s vocabulary."""
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
    """A materialized coarse level as (c, y, x), for the mini-map and stats.

    The same heuristic as every other format's -- the smallest level with both
    dimensions >= `minimum`, pooled down when it is still well above it -- which
    is what makes it bounded whatever the slide's full resolution is. The dtype
    survives for a fluorescence slide, because the quantization windows are
    computed from these numbers and a uint16 panel squeezed into uint8 first
    would compute them from the wrong ones.
    """
    from skimage.measure import block_reduce

    candidates = [index for index in range(len(pyramid))
                  if all(d >= minimum for d in pyramid[index].shape[-2:])]
    index = candidates[-1] if candidates else 0
    array = np.asarray(pyramid[index])
    if array.shape[-2] > maximum or array.shape[-1] > maximum:
        factor = int(min(array.shape[-2] // minimum, array.shape[-1] // minimum))
        if factor > 1:
            pooled = block_reduce(array, (1, factor, factor), np.mean)
            array = pooled.astype(array.dtype)
    return array


def physical_metadata(pyramid) -> dict:
    """`{physical_size_x, physical_size_x_unit, ...}` for the viewer's scale bar.

    The same two keys `/get_ome_metadata` serves for an OME-TIFF, so the scale
    bar needs no knowledge of where the numbers came from. An empty dict when
    the slide declares no pixel spacing, which is the state the scale bar
    already hides itself for.
    """
    mpp = getattr(pyramid, "mpp", None)
    if not mpp or not mpp[0]:
        return {}
    return {"physical_size_x": float(mpp[0]), "physical_size_x_unit": "µm",
            "physical_size_y": float(mpp[1] or mpp[0]), "physical_size_y_unit": "µm"}


def needs_extension(pyramid) -> bool:
    """Whether the levels that can be served reach far enough out.

    False for any slide whose own pyramid covers the zoom range, which for
    DICOM WSI is the overwhelming majority -- the format exists to be tiled and
    exporters write the full chain. Asked as "did the chain reach one tile", the
    same question `brightfield.needs_extension` asks and for the same reason:
    a truncated virtual chain still advertises its coarse levels, and every
    tile of them would resample a rectangle the size of the whole slide.
    """
    height, width = pyramid.level_shapes[0]
    return len(pyramid) < len(brightfield._dyadic_shapes(height, width))


def extension_path(data_directory) -> Path:
    """Where a DICOM slide's derived levels live."""
    return Path(data_directory) / "dicom_pyramid.zarr"


def build_extension(pyramid, dest, progress_callback=None) -> Optional[str]:
    """Derive the coarse levels `pyramid` cannot serve, into a zarr store.

    Delegates to `ome_zarr.build_extension` unchanged: the CYX level views above
    are already the input contract it wants (`.shape` as (c, y, x),
    `[channel, rows]` slicing, a `.dtype`), and it names its output arrays by
    absolute level index, which `open_image` appends by the same number.
    """
    from plexora.server.utils import ome_zarr

    return ome_zarr.build_extension(pyramid, dest,
                                    progress_callback=progress_callback)


__all__ = [
    "DICOM_SUFFIXES",
    "DicomPyramid",
    "DicomSupportMissing",
    "IMAGE_KIND",
    "SlideSource",
    "TILE_SIZE",
    "WSI_SOP_CLASS_UID",
    "assemble_slide",
    "build_extension",
    "channel_names",
    "detect_image_type",
    "extension_path",
    "geometry",
    "is_dicom_path",
    "needs_extension",
    "open_image",
    "overview_plane",
    "physical_metadata",
    "slide_instances",
]
