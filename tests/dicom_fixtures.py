"""Hand-written DICOM whole-slide instances for the DICOM tests.

Built with pydicom directly and never with wsidicom's own writer, for the same
reason `brightfield_fixtures.py` writes its TIFFs by hand: a fixture produced by
the library under test proves that the library round-trips, which is not the
question. The question is whether Plexora reconstructs a *slide* out of files
that only describe themselves -- so these are written the way real exporters
write, one optical path per instance and one instance per pyramid level, with
the identity of the slide living entirely in Container Identifier, Frame of
Reference and the study/series UIDs.

Everything here is small and legible at the level the tests check: a dark field
with one bright blob per marker for fluorescence, a pale field with a stained
patch for H&E. The blobs are in different places per channel, which is what lets
a test assert that channel 2 really is channel 2 and not channel 0 read twice.

Uncompressed Explicit VR Little Endian throughout. Transfer syntaxes are
wsidicom's problem, not Plexora's, and an uncompressed fixture is one whose
expected pixel values a test can state exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

#: VL Whole Slide Microscopy Image Storage.
WSI_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.77.1.6"

#: Background of a fluorescence image: mostly nothing, and dark.
_DARK = 8
#: Background of a transmitted-light image: mostly slide, and bright.
_LIGHT = 236

#: The tile the fixtures are written in. Smaller than Plexora's virtual 1024
#: grid on purpose -- a DICOM instance's own tiling is its own business, and a
#: fixture that shared the viewer's number would hide any assumption that they
#: are the same.
TILE = 64


@dataclass
class SlideIds:
    """The identifiers that make a set of files one slide.

    A slide is not a directory and not a series; it is whatever shares these.
    Held together in one object so a test can write two slides into one folder
    and be sure they differ in the way that matters.
    """

    container: str = "SLIDE-1"
    study: str = field(default_factory=generate_uid)
    series: str = field(default_factory=generate_uid)
    frame_of_reference: str = field(default_factory=generate_uid)
    pyramid: str = field(default_factory=generate_uid)


def emitted(height, width, channel=0, channels=1, dtype=np.uint16):
    """(y, x) -- a dark field with one bright blob, placed by channel index."""
    ceiling = int(np.iinfo(dtype).max)
    plane = np.full((height, width), _DARK, dtype=dtype)
    top = (channel + 1) * height // (channels + 2)
    left = (channel + 1) * width // (channels + 2)
    plane[top:top + height // 8, left:left + width // 8] = ceiling // 2
    return plane


def stained(height, width):
    """(y, x, 3) uint8 -- a pale field with one haematoxylin-and-eosin patch."""
    image = np.full((height, width, 3), _LIGHT, dtype=np.uint8)
    image[height // 4:height // 2, width // 4:width // 2] = (150, 92, 172)
    image[height // 2:height * 3 // 4, width // 3:width * 2 // 3] = (214, 138, 156)
    return image


def _tiles(pixels, tile=TILE):
    """`pixels` cut into the row-major frame grid TILED_FULL defines.

    The last row and column are padded rather than short: every frame of a
    DICOM instance is the same size, and the total pixel matrix says where the
    real pixels stop.
    """
    height, width = pixels.shape[:2]
    down = -(-height // tile)
    across = -(-width // tile)
    shape = (tile, tile) + pixels.shape[2:]
    frames = []
    for row in range(down):
        for column in range(across):
            frame = np.zeros(shape, dtype=pixels.dtype)
            block = pixels[row * tile:(row + 1) * tile,
                           column * tile:(column + 1) * tile]
            frame[:block.shape[0], :block.shape[1]] = block
            frames.append(frame)
    return frames, down, across


def _code(value, scheme, meaning) -> Dataset:
    item = Dataset()
    item.CodeValue = value
    item.CodingSchemeDesignator = scheme
    item.CodeMeaning = meaning
    return item


def _text_item(concept, text) -> Dataset:
    item = Dataset()
    item.ValueType = "TEXT"
    item.ConceptNameCodeSequence = [concept]
    item.TextValue = text
    return item


def _optical_path(identifier, description=None, wavelength=None,
                  color=False) -> Dataset:
    path = Dataset()
    path.OpticalPathIdentifier = str(identifier)
    if description is not None:
        path.OpticalPathDescription = description
    if wavelength is not None:
        path.IlluminationWaveLength = float(wavelength)
    path.IlluminationTypeCodeSequence = [
        _code("111744", "DCM", "Brightfield illumination") if color
        else _code("111743", "DCM", "Epifluorescence illumination")]
    path.IlluminationColorCodeSequence = [
        _code("414298005", "SCT", "Full spectrum") if color
        else _code("134223000", "SCT", "Narrow")]
    return path


def _staining_steps(container, markers) -> Dataset:
    """A specimen description whose preparation record names each channel.

    The second naming tier: `{Channel: "2"} -> {Component investigated: "CD3"}`,
    which is how an exporter that leaves Optical Path Description blank still
    says what it stained for. Written only by `write_stained_only_slide`.
    """
    description = Dataset()
    description.SpecimenIdentifier = container
    description.SpecimenUID = generate_uid()
    description.IssuerOfTheSpecimenIdentifierSequence = []
    steps = []
    for identifier, marker in markers.items():
        step = Dataset()
        content = [
            _text_item(_code("121041", "DCM", "Specimen Identifier"), container),
        ]
        processing = Dataset()
        processing.ValueType = "CODE"
        processing.ConceptNameCodeSequence = [
            _code("111701", "DCM", "Processing type")]
        processing.ConceptCodeSequence = [_code("127790008", "SCT", "Staining")]
        content.append(processing)
        content.append(_text_item(_code("C44170", "NCIt", "Channel"),
                                  str(identifier)))
        content.append(_text_item(
            _code("246094008", "SCT", "Component investigated"), marker))
        step.SpecimenPreparationStepContentItemSequence = content
        steps.append(step)
    description.SpecimenPreparationSequence = steps
    return description


def _write_instance(path, pixels, ids, *, optical_path, flavor="VOLUME",
                    focal_planes=1, mpp=0.5, specimen=None, tile=TILE,
                    total_size=None, sparse=False):
    """One `.dcm` file holding one optical path of one resolution level.

    `total_size` overrides the total pixel matrix dimensions, which is how a
    level whose own pixels were written smaller still claims the size its
    pyramid position implies -- not used by the fixtures, but the parameter is
    what makes the writer honest about the two being different facts.
    """
    path = Path(path)
    color = pixels.ndim == 3
    frames, down, across = _tiles(pixels, tile)
    height, width = (total_size or pixels.shape[:2])

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = WSI_SOP_CLASS
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)

    dataset.SOPClassUID = WSI_SOP_CLASS
    dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    dataset.StudyInstanceUID = ids.study
    dataset.SeriesInstanceUID = ids.series
    dataset.FrameOfReferenceUID = ids.frame_of_reference
    dataset.PyramidUID = ids.pyramid
    dataset.ContainerIdentifier = ids.container
    dataset.IssuerOfTheContainerIdentifierSequence = []
    dataset.ContainerTypeCodeSequence = [
        _code("433466003", "SCT", "Microscope slide")]

    dataset.Modality = "SM"
    dataset.ImageType = ["ORIGINAL", "PRIMARY", flavor, "NONE"]
    dataset.VolumetricProperties = "VOLUME"
    dataset.SeriesNumber = 1
    dataset.InstanceNumber = 1
    dataset.StudyID = "1"
    dataset.AccessionNumber = ""
    dataset.PatientName = "Fixture^Slide"
    dataset.PatientID = "FIXTURE"
    dataset.PatientBirthDate = ""
    dataset.PatientSex = ""
    dataset.ReferringPhysicianName = ""
    dataset.Manufacturer = "Plexora tests"
    dataset.ManufacturerModelName = "dicom_fixtures"
    dataset.DeviceSerialNumber = "0"
    dataset.SoftwareVersions = "0"
    dataset.StudyDate = dataset.SeriesDate = dataset.ContentDate = "20200101"
    dataset.StudyTime = dataset.SeriesTime = dataset.ContentTime = "000000"
    dataset.AcquisitionDateTime = "20200101000000"

    dataset.SamplesPerPixel = 3 if color else 1
    dataset.PhotometricInterpretation = "RGB" if color else "MONOCHROME2"
    if color:
        dataset.PlanarConfiguration = 0
    dataset.BitsAllocated = 8 if color else 16
    dataset.BitsStored = dataset.BitsAllocated
    dataset.HighBit = dataset.BitsAllocated - 1
    dataset.PixelRepresentation = 0
    dataset.Rows = tile
    dataset.Columns = tile
    dataset.NumberOfFrames = len(frames) * focal_planes
    dataset.BurnedInAnnotation = "NO"
    dataset.LossyImageCompression = "00"
    dataset.PresentationLUTShape = "IDENTITY"

    dataset.TotalPixelMatrixColumns = int(width)
    dataset.TotalPixelMatrixRows = int(height)
    dataset.TotalPixelMatrixFocalPlanes = int(focal_planes)
    dataset.NumberOfOpticalPaths = 1
    dataset.OpticalPathSequence = [optical_path]
    dataset.ImagedVolumeWidth = float(width * mpp / 1000.0)
    dataset.ImagedVolumeHeight = float(height * mpp / 1000.0)
    dataset.ImagedVolumeDepth = 1.0
    dataset.SpecimenLabelInImage = "NO"
    dataset.FocusMethod = "AUTO"
    dataset.ExtendedDepthOfField = "NO"
    dataset.ImageOrientationSlide = [0, -1, 0, -1, 0, 0]

    origin = Dataset()
    origin.XOffsetInSlideCoordinateSystem = 0.0
    origin.YOffsetInSlideCoordinateSystem = 0.0
    dataset.TotalPixelMatrixOriginSequence = [origin]

    dataset.SpecimenDescriptionSequence = [specimen or _plain_specimen(ids)]

    organization = Dataset()
    organization.DimensionOrganizationUID = generate_uid()
    dataset.DimensionOrganizationSequence = [organization]
    dataset.DimensionOrganizationType = "TILED_SPARSE" if sparse else "TILED_FULL"

    measures = Dataset()
    measures.PixelSpacing = [f"{mpp / 1000.0:.9f}", f"{mpp / 1000.0:.9f}"]
    measures.SliceThickness = "0.001"
    measures.SpacingBetweenSlices = "0.001"
    shared = Dataset()
    shared.PixelMeasuresSequence = [measures]
    dataset.SharedFunctionalGroupsSequence = [shared]

    if sparse:
        # TILED_SPARSE states every frame's position explicitly instead of
        # implying it from the row-major order, which is the whole difference
        # between the two organizations and the reason a reader has to be told
        # apart from a grid walker.
        per_frame = []
        for plane in range(focal_planes):
            for index in range(len(frames)):
                row, column = divmod(index, across)
                position = Dataset()
                position.ColumnPositionInTotalImagePixelMatrix = column * tile + 1
                position.RowPositionInTotalImagePixelMatrix = row * tile + 1
                position.XOffsetInSlideCoordinateSystem = float(column * tile * mpp / 1000.0)
                position.YOffsetInSlideCoordinateSystem = float(row * tile * mpp / 1000.0)
                position.ZOffsetInSlideCoordinateSystem = float(plane)
                item = Dataset()
                item.PlanePositionSlideSequence = [position]
                per_frame.append(item)
        dataset.PerFrameFunctionalGroupsSequence = per_frame

    # Focal planes repeat the whole grid, which is what makes a z-stack look
    # like more frames rather than more channels -- the mistake the reader is
    # built not to make.
    payload = b"".join(frame.tobytes() for _ in range(focal_planes)
                       for frame in frames)
    dataset.PixelData = payload
    dataset.save_as(str(path), enforce_file_format=False)
    return path


def _plain_specimen(ids) -> Dataset:
    description = Dataset()
    description.SpecimenIdentifier = ids.container
    description.SpecimenUID = generate_uid()
    description.IssuerOfTheSpecimenIdentifierSequence = []
    description.SpecimenPreparationSequence = []
    return description


#: The panel the multiplex fixtures carry. Three markers a person would
#: recognise, so a failure reads as "CD3 is missing" rather than "index 1".
MARKERS = ("DNA", "CD3", "Ki67")


def write_if_slide(directory, *, markers=MARKERS, height=256, width=320,
                   levels=1, ids=None, nested=False, describe=True,
                   focal_planes=1, sparse=False, mpp=0.5):
    """A multiplex fluorescence slide: one instance per (level, marker).

    The layout every real multiplex export has and the one Plexora exists to
    reconstruct -- N files that are N markers, not N images. `nested` puts them
    under `<study>/<series>/` subdirectories the way an HTAN export does, which
    is the case a one-level directory scan misses.
    """
    directory = Path(directory)
    ids = ids or SlideIds()
    target = directory
    if nested:
        target = directory / "study" / "series"
    target.mkdir(parents=True, exist_ok=True)

    for level in range(levels):
        step = 2 ** level
        level_height = -(-height // step)
        level_width = -(-width // step)
        for index, marker in enumerate(markers):
            plane = emitted(level_height, level_width, index, len(markers))
            path = _optical_path(
                index,
                description=marker if describe else None,
                wavelength=488 + 100 * index)
            _write_instance(
                target / f"level{level}_path{index}.dcm", plane, ids,
                optical_path=path, focal_planes=focal_planes, sparse=sparse,
                mpp=mpp * step)
    return directory


def write_he_slide(directory, *, height=256, width=320, levels=1, ids=None):
    """A brightfield H&E slide: one colour instance per level.

    RGB samples in one plane, which is what makes it one picture rather than
    three markers -- the distinction the whole reader is arranged around.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ids = ids or SlideIds(container="HE-SLIDE")
    for level in range(levels):
        step = 2 ** level
        pixels = stained(-(-height // step), -(-width // step))
        _write_instance(directory / f"he_level{level}.dcm", pixels, ids,
                        optical_path=_optical_path(0, "Brightfield", color=True),
                        mpp=0.5 * step)
    return directory


def write_associated_images(directory, *, ids, height=128, width=128):
    """The label and overview photographs that ride along with a slide.

    Pictures of the glass and its barcode, not of the specimen. They share every
    identifier the volume instances have, which is exactly why "same slide" and
    "part of the image" have to be different questions.
    """
    directory = Path(directory)
    for flavor in ("LABEL", "OVERVIEW"):
        _write_instance(directory / f"{flavor.lower()}.dcm",
                        emitted(height, width), ids,
                        optical_path=_optical_path(0, "Label", color=False),
                        flavor=flavor)
    return directory


def write_stained_only_slide(directory, *, markers=MARKERS, height=256,
                             width=320, ids=None):
    """A slide whose optical paths are unnamed but whose staining record is not.

    The second naming tier on its own: Optical Path Description is absent, so
    the markers can only come from the specimen preparation steps.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ids = ids or SlideIds(container="STAINED")
    specimen = _staining_steps(ids.container,
                               {str(i): m for i, m in enumerate(markers)})
    for index, marker in enumerate(markers):
        _write_instance(
            directory / f"path{index}.dcm",
            emitted(height, width, index, len(markers)), ids,
            optical_path=_optical_path(index, description=None,
                                       wavelength=488 + 100 * index),
            specimen=specimen)
    return directory


def write_unnamed_slide(directory, *, markers=MARKERS, height=256, width=320,
                        ids=None):
    """A slide that says nothing about its channels but their wavelengths.

    The third tier, and the floor below it: a test asserts the names fall back
    rather than coming out half-empty.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ids = ids or SlideIds(container="ANONYMOUS")
    for index in range(len(markers)):
        _write_instance(
            directory / f"path{index}.dcm",
            emitted(height, width, index, len(markers)), ids,
            optical_path=_optical_path(index, description=None,
                                       wavelength=488 + 100 * index))
    return directory


def write_two_slides(directory, **kwargs):
    """Two slides in one folder, differing only in Container Identifier.

    The case Plexora refuses to guess about. They share a study, because two
    slides from one block normally do -- which is why the study alone cannot be
    the grouping key.
    """
    directory = Path(directory)
    study = generate_uid()
    first = SlideIds(container="SLIDE-A", study=study)
    second = SlideIds(container="SLIDE-B", study=study)
    write_if_slide(directory / "a", ids=first, **kwargs)
    write_if_slide(directory / "b", ids=second, **kwargs)
    return directory, first, second
