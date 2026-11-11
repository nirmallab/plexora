"""OME-XML read for what a viewer needs, whatever schema wrote it.

`ome_types` models one schema (2016-06) and converts older documents to it
with lxml, which Plexora does not ship: a file written under any schema from
2003-FC to 2015-01 -- every Bio-Formats, MetaMorph, Leica or Olympus export
before about 2016 -- came back from it as an exception, which the callers
swallowed, and the image opened with "Channel 1, Channel 2" for a panel and no
scale bar. The few facts a viewer reads have barely moved in twenty years of
schemas, so they are read here with ElementTree by local tag name, from any
namespace:

- **Channel names.** `Pixels/Channel@Name` since 2009-09; before that
  `Image/LogicalChannel@Name` (2007-06 to 2008-09) and `Image/ChannelInfo@Name`
  (2003-FC), each naming the channels its `ChannelComponent@Index` children
  list.
- **Pixel size.** `Pixels@PhysicalSizeX/Y/Z`, in microns unless
  `PhysicalSizeXUnit` (2015-01 on) says otherwise; `Image@PixelSizeX/Y/Z` in
  2003-FC.
- **The files of the image.** `TiffData/UUID@FileName` (one file per channel,
  plane or timepoint: Bio-Formats, Micro-Manager, a Xenium focus folder) and
  `BinaryOnly@MetadataFile` (the file that holds the metadata for this one).

Only the **first** `Image` is the image Plexora shows; a document's later ones
are a label, a macro, another scene, or -- before the 2019 pyramid
specification -- the same image again at each lower resolution, with a pixel
size to match. Parsing stops at the end of the images asked for, so a document
that carries megabytes of `OriginalMetadata` after them costs nothing.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from xml.etree import ElementTree

__all__ = ["Image", "Document", "read", "channel_names", "pixels", "is_ome_xml"]


@dataclass
class Image:
    name: str = ""
    size_x: int = 0
    size_y: int = 0
    size_z: int = 1
    size_c: int = 1
    size_t: int = 1
    dimension_order: str = ""
    pixel_type: str = ""
    big_endian: bool | None = None
    physical_size: dict = field(default_factory=dict)   # {"x": (value, unit), ...}
    time_increment: tuple | None = None
    channels: list = field(default_factory=list)        # [{"name", "samples"}]
    #: `(first_c, first_z, first_t, ifd, plane_count, file_name, uuid)` per
    #: TiffData element, in document order; a field the element leaves out is
    #: None.
    tiff_data: list = field(default_factory=list)

    @property
    def samples(self) -> list:
        return [int(channel.get("samples") or 1) for channel in self.channels]

    @property
    def files(self) -> list:
        """The file names the planes are in, in first-use order, without
        repeats; empty when every plane is in the file that holds the XML."""
        seen: list = []
        for entry in self.tiff_data:
            if entry[5] and entry[5] not in seen:
                seen.append(entry[5])
        return seen


@dataclass
class Document:
    images: list = field(default_factory=list)
    uuid: str = ""
    #: `BinaryOnly@MetadataFile`: this file holds pixels only, and that one
    #: (a `.companion.ome` or another OME-TIFF beside it) describes them.
    metadata_file: str = ""
    schema: str = ""


#: The root element, with or without a namespace prefix (`<ome:OME ...>`).
_ROOT = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?OME[\s>]")
_ROOT_BYTES = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?OME[\s>]")


def is_ome_xml(description) -> bool:
    return bool(description) and _ROOT.search(str(description)) is not None


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value):
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number else None     # NaN is no answer


_SCHEMA = re.compile(r"/Schemas/OME/(\d{4}-\d{2})|/XMLschemas/OME/(FC)/")


def _as_bytes(description) -> bytes:
    if isinstance(description, bytes):
        return description
    text = str(description)
    # An encoding declaration is a statement about bytes; the text is already
    # decoded, and ElementTree refuses a str that carries one for another
    # encoding (PerkinElmer writes utf-16 there).
    return text.encode("utf-8")


def _strip_declaration(data: bytes) -> bytes:
    start = data.lstrip()
    if start.startswith(b"<?xml"):
        end = start.find(b"?>")
        if end != -1:
            return start[end + 2:]
    return start


def read(description, images: int = 1) -> Document | None:
    """The document's first `images` images, or None when it is not OME-XML
    or cannot be parsed far enough to find one."""
    if not description:
        return None
    data = _strip_declaration(_as_bytes(description))
    if _ROOT_BYTES.search(data) is None:
        return None
    document = Document()
    found = _SCHEMA.search(data[:4096].decode("utf-8", "replace"))
    if found:
        document.schema = "2003-FC" if found.group(2) else found.group(1)

    depth = 0
    image_depth = None
    try:
        for event, element in ElementTree.iterparse(io.BytesIO(data), events=("start", "end")):
            tag = _local(element.tag)
            if event == "start":
                depth += 1
                if depth == 1:
                    if tag != "OME":
                        return None
                    document.uuid = element.get("UUID") or ""
                elif depth == 2 and tag == "Image":
                    image_depth = depth
                continue
            depth -= 1
            if tag == "BinaryOnly" and depth == 1:
                document.metadata_file = element.get("MetadataFile") or ""
                if not document.uuid:
                    document.uuid = element.get("UUID") or ""
            elif tag == "Image" and image_depth is not None and depth == image_depth - 1:
                document.images.append(_image(element))
                image_depth = None
                element.clear()
                if len(document.images) >= images:
                    break
            elif depth == 1 and image_depth is None:
                element.clear()         # an instrument, an annotation block: not kept
    except ElementTree.ParseError:
        # A truncated or malformed block still answers for the images that
        # closed before the damage.
        if not document.images and not document.metadata_file:
            return None
    return document


def _image(element) -> Image:
    image = Image(name=element.get("Name") or "")
    pixels = None
    named: list = []        # (name, samples, [component indexes]) from the pre-2009 elements
    for child in element:
        tag = _local(child.tag)
        if tag == "Pixels" and pixels is None:
            pixels = child
        elif tag in ("LogicalChannel", "ChannelInfo"):
            indexes = [_int(part.get("Index")) for part in child if _local(part.tag) == "ChannelComponent"]
            named.append((child.get("Name") or "", _int(child.get("SamplesPerPixel"), 1) or 1,
                          [index for index in indexes if index is not None]))
    if pixels is None:
        return image

    image.size_x = _int(pixels.get("SizeX"), 0) or 0
    image.size_y = _int(pixels.get("SizeY"), 0) or 0
    image.size_z = _int(pixels.get("SizeZ"), 1) or 1
    image.size_c = _int(pixels.get("SizeC"), 1) or 1
    image.size_t = _int(pixels.get("SizeT"), 1) or 1
    image.dimension_order = (pixels.get("DimensionOrder") or "").upper()
    image.pixel_type = (pixels.get("Type") or pixels.get("PixelType") or "").lower()
    endian = pixels.get("BigEndian")
    image.big_endian = None if endian is None else endian.strip().lower() == "true"

    for axis in "XYZ":
        value = _float(pixels.get(f"PhysicalSize{axis}"))
        unit = pixels.get(f"PhysicalSize{axis}Unit") or "µm"
        if value is None:                       # 2003-FC states it on the Image
            value = _float(element.get(f"PixelSize{axis}"))
            unit = "µm"
        if value is not None and value > 0:
            image.physical_size[axis.lower()] = (value, unit)
    step = _float(pixels.get("TimeIncrement"))
    if step is not None and step > 0:
        image.time_increment = (step, pixels.get("TimeIncrementUnit") or "s")

    for child in pixels:
        tag = _local(child.tag)
        if tag == "Channel":
            image.channels.append({"name": child.get("Name") or "",
                                   "samples": _int(child.get("SamplesPerPixel"), 1) or 1,
                                   "color": child.get("Color"),
                                   "fluor": child.get("Fluor") or ""})
        elif tag == "TiffData":
            file_name = uuid = None
            for part in child:
                if _local(part.tag) == "UUID":
                    file_name = part.get("FileName")
                    uuid = (part.text or "").strip() or None
            count = child.get("PlaneCount") or child.get("NumPlanes")
            image.tiff_data.append((
                _int(child.get("FirstC")), _int(child.get("FirstZ")), _int(child.get("FirstT")),
                _int(child.get("IFD")), _int(count), file_name, uuid))

    if not image.channels and named:
        image.channels = _named_channels(named, image.size_c)
    return image


def _named_channels(named, size_c) -> list:
    """The 2003-FC to 2008-09 spelling: each `LogicalChannel` (`ChannelInfo`)
    names the channels its `ChannelComponent@Index` children list; one that
    lists none stands for the next channel not yet named."""
    width = max([size_c] + [index + 1 for _name, _samples, indexes in named for index in indexes])
    channels = [None] * width
    for name, samples, indexes in named:
        if not indexes:
            free = next((slot for slot, taken in enumerate(channels) if taken is None), None)
            indexes = [] if free is None else [free]
        for position, index in enumerate(indexes):
            # One logical channel over several components (an RGB camera, a
            # spectral detector): the components are told apart by number.
            label = name if len(indexes) == 1 or not name else f"{name} {position + 1}"
            channels[index] = {"name": label, "samples": samples, "color": None, "fluor": ""}
    return [entry or {"name": "", "samples": 1, "color": None, "fluor": ""} for entry in channels]


def channel_names(description) -> list:
    """The first image's channel names, one per channel, '' where the file
    names none; [] when it is not OME-XML."""
    document = read(description)
    if document is None or not document.images:
        return []
    return [str(channel.get("name") or "") for channel in document.images[0].channels]


def pixels(description) -> dict:
    """The first image's Pixels as the flat mapping every other format's
    reader hands the scale bar -- the keys `ome_types`' own model dumps to, so
    a caller that falls back to this from it serves the same payload."""
    document = read(description)
    if document is None or not document.images:
        return {}
    image = document.images[0]
    out: dict = {
        "size_x": image.size_x, "size_y": image.size_y, "size_z": image.size_z,
        "size_c": image.size_c, "size_t": image.size_t,
        "dimension_order": image.dimension_order, "type": image.pixel_type,
        "channels": [{"name": channel.get("name") or None,
                      "samples_per_pixel": channel.get("samples") or 1}
                     for channel in image.channels],
    }
    for axis, (value, unit) in image.physical_size.items():
        out[f"physical_size_{axis}"] = value
        out[f"physical_size_{axis}_unit"] = unit
    if image.time_increment:
        out["time_increment"], out["time_increment_unit"] = image.time_increment
    return out
