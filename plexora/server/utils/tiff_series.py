"""The channel image's `series[0]`, presented as (channel, y, x).

Every reader in Plexora that opens a multiplex TIFF indexes `series[0]`
positionally -- `shape[0]` is the channel count, `shape[1]` the height,
`shape[2]` the width -- and none of them looks at what the series' axes
actually are. That is right for the two layouts the format usually arrives in,
a plain multi-page stack and a pyramidal OME-TIFF, which are both `CYX`. It is
wrong for the third, which is common enough to matter.

An **ImageJ hyperstack** stores its planes as `TCYX` or `ZCYX`: a CODEX cycle
stack of 23 cycles x 4 channels is one series of shape `(23, 4, 1440, 1920)`,
not `(92, 1440, 1920)`. Read positionally that registers as 23 channels, four
pixels tall and 1440 wide -- and then fails to load at all, because the
overview heuristic in `providers/local.py` is looking for a level whose
non-channel dimensions are at least 200 and a four-pixel axis has no such
level. QuPath opens the same file correctly because Bio-Formats reads the
ImageJ metadata rather than the shape.

`channel_series` is the one place that reads `series.axes`, and it is
deliberately conservative: a series that is already `(C, Y, X)` is handed back
**unchanged**, so the common path does not move at all. The flattened case is
built as a real `tifffile.TiffPageSeries`, which is what keeps this a
five-line change rather than a rewrite -- `aszarr()` on it still returns a
genuine `zarr.Array`, so `read_tile`'s isinstance branch, `_zarr_level`,
`quantization_window_of` and `node/api.py`'s `hasattr(pyramid, "shape")` test
all take exactly the paths they took before.

**Page order is the flattening order.** A series' shape is derived from the
order its pages are stored in, so collapsing the leading axes row-major is the
same walk as reading the pages start to finish -- for `TCYX` that is cycle 1's
four channels, then cycle 2's, which is also the order an Akoya
`channelNames.txt` lists its panel in. Nothing here has to know that; it falls
out of using the pages themselves rather than re-deriving an index.

A **Z-stack** is the third layout, and the one a Xenium run ships. Its planes
are focal depths of ONE channel, not channels: `morphology.ome.tif` is 14
planes of DAPI, each autofocused per field of view, so read positionally it
registers as fourteen "channels" that each light a different block of tissue.
Nothing about the shape says which it is -- `(14, Y, X)` is the same array
either way -- so the file is asked what it holds (`plane_sizes`: OME-XML's
`SizeC=1 SizeZ=14` first, an ImageJ header's `channels`/`slices`/`frames`
second), and a single-channel stack collapses to its MIDDLE plane. That is the
rule `dicom_wsi` already applies to a DICOM z-stack, and it is recorded the
same way: `focalPlanes`/`focalPlane` on the channel info.

A file that describes itself neither way keeps the reading it has always had.
That is not timidity: `SizeZ > 1` on its own does not mean focus -- an Akoya
export puts its CYCLES on the Z axis -- so what makes the collapse safe is the
file saying, in its own words, that it has one channel.

Masks are **not** routed through this. A label image is a single 2-D plane and
`read_tile` indexes it with two subscripts, so presenting one as `(1, Y, X)`
would break the reader that is currently correct.
"""

import functools
import os
import re
from collections import OrderedDict
from xml.etree import ElementTree

import tifffile as tf


def _level0_samples(series) -> int:
    total = 1
    for side in series.shape:
        total *= int(side)
    return total


def _is_reduced(series) -> bool:
    try:
        return bool(int(series.pages[0].subfiletype) & 1)
    except Exception:
        return False


def _plane(series):
    axes = str(getattr(series, "axes", "") or "")
    shape = tuple(int(side) for side in series.shape)
    if "Y" not in axes or "X" not in axes or len(axes) != len(shape):
        return None
    return shape[axes.index("Y")], shape[axes.index("X")]


def _adopt_reduced_levels(series):
    """`series` with the first image's reduced-resolution pages chained to it
    as its levels, when tifffile left them as images of their own.

    tifffile chains a pyramid's levels only when each is the one before
    divided by exactly 2, 3 or 4. A pyramid whose steps are anything else --
    libvips and GDAL overviews of an odd-sized image, a scanner's own
    reductions -- arrives as a flat full-resolution image followed by
    unrelated smaller ones, and Plexora then derived the coarse levels it
    already had by reading the whole base: half a minute for a 1.5-gigapixel
    slide, where using the file's own levels is no work at all. The pages
    say what they are: `NewSubfileType` bit 0, "a reduced-resolution version
    of another image in this file" -- OpenSlide's rule for a generic
    pyramidal TIFF. Adopted only while each is smaller than the last, of the
    same layout, and the same picture (aspect ratio within 1%, which leaves
    out a format whose levels overlap their tiles differently). The readers
    lay a halving chain over uneven steps (`needs_region_reader`,
    `brightfield.open_rgb`).
    """
    base = series[0]
    if _is_reduced(base) or len(getattr(base, "levels", None) or [base]) > 1:
        return series
    plane = _plane(base)
    if plane is None:
        return series
    chain, rest = [base], []
    for position, entry in enumerate(series[1:], start=1):
        size = _plane(entry)
        last = _plane(chain[-1])
        if (size is not None and not rest and _is_reduced(entry)
                and entry.axes == base.axes and entry.dtype == base.dtype
                and len(entry.shape) == len(base.shape)
                and [d for d, a in zip(entry.shape, entry.axes) if a not in "YX"]
                == [d for d, a in zip(base.shape, base.axes) if a not in "YX"]
                and size[0] < last[0] and size[1] < last[1]
                and abs((size[1] / size[0]) / (plane[1] / plane[0]) - 1) <= 0.01):
            chain.extend(getattr(entry, "levels", None) or [entry])
        else:
            rest.append(entry)
    if len(chain) == 1:
        return series
    base.levels = chain
    return [base] + rest


def arrange_series(tiff, series):
    """`series` with the image a person means by "this file" first.

    Every reader takes `series[0]`, and for nearly every file that is the
    image. Two layouts put something else there:

    - **Leica SCN** lists the slide's macro overview first (the whole slide
      at about 16 um a pixel) and the scanned regions after it, so an SCN
      opened as a 1616 x 4668 thumbnail of the slide with its label. The
      image is the largest region; a fluorescence scan's is the largest too,
      one page a channel.
    - A file whose **first page is a preview** flagged reduced-resolution
      (`NewSubfileType` bit 0) with the data after it: an Imaris 3/4 "classic"
      `.ims`, which is a TIFF whose first directory is a 512 x 512 RGB
      thumbnail.

    A pyramid tifffile did not chain is chained first
    (`_adopt_reduced_levels`). Otherwise the list is reordered, never
    filtered: the other series stay reachable after the first.
    """
    if len(series) < 2:
        return series
    series = _adopt_reduced_levels(series)
    if len(series) < 2:
        return series
    chosen = 0
    if getattr(tiff, "is_scn", False):
        chosen = max(range(len(series)), key=lambda index: _level0_samples(series[index]))
    elif _is_reduced(series[0]):
        full = [index for index in range(len(series)) if not _is_reduced(series[index])]
        if full:
            chosen = max(full, key=lambda index: _level0_samples(series[index]))
    if chosen:
        series = [series[chosen]] + [entry for index, entry in enumerate(series) if index != chosen]
    return series


_PLAIN_SERIES = tf.TiffFile.__dict__.get("series")
_PLAIN_SERIES_READ = getattr(_PLAIN_SERIES, "func", None) or getattr(_PLAIN_SERIES, "fget", None)


class ImageTiff(tf.TiffFile):
    """`tifffile.TiffFile` for an IMAGE (never a mask), opened as Plexora
    opens every TIFF (`is_ome=False` unless the caller says otherwise), whose
    `series[0]` is the image itself (`arrange_series`). Every reader that
    opens a TIFF to show it constructs this rather than `tf.TiffFile`, so
    they all agree on which series that is."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("is_ome", False)
        super().__init__(*args, **kwargs)

    @functools.cached_property
    def series(self):
        if _PLAIN_SERIES_READ is None:      # a tifffile that stores it some other way
            return arrange_series(self, list(super().series))
        return arrange_series(self, list(_PLAIN_SERIES_READ(self)))


#: How much of `ImageDescription` is scanned for the sign that it is OME-XML.
#: Generous rather than exact: the marker is the namespace on the root element,
#: which sits a few hundred characters in for every writer anyone has met --
#: but a `<?xml?>` prolog, a BOM, a comment or a long `UUID` attribute all push
#: it further, and a header this reader failed to recognise is a Z-stack served
#: as a panel of unrelated-looking channels. Parsing is still what decides; this
#: only keeps a megabyte of somebody's ImageJ notes out of the XML parser.
OME_MARKER_WINDOW = 4096


def _ome_sizes(tiff):
    """`{SizeC, SizeZ, SizeT}` out of the file's own OME-XML, or None.

    Read with ElementTree rather than `ome_types` because this runs on the way
    to every tile-serving open: three attributes off one element is a few
    microseconds, and validating the whole document to get them is not.

    The XML is taken from page 0's `ImageDescription`, which is where it lives
    -- Plexora opens every TIFF with `is_ome=False` (an OME reader would
    re-interpret a label image's metadata), so `tiff.ome_metadata` is None
    however OME the file is.
    """
    try:
        xml = tiff.pages[0].tags["ImageDescription"].value
    except Exception:
        return None
    if not isinstance(xml, str):
        return None
    head = xml[:OME_MARKER_WINDOW]
    # Either marker, because they fail independently: a document written
    # against a schema copy on some institution's own host still opens with
    # `<OME`, and one whose root element is namespace-prefixed
    # (`<ome:OME xmlns:ome="...openmicroscopy...">`) still carries the URL.
    if "openmicroscopy" not in head and "<OME" not in head:
        return None
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "Pixels":
            continue
        sizes = {}
        for key in ("SizeC", "SizeZ", "SizeT"):
            try:
                sizes[key] = int(element.attrib.get(key, 1))
            except (TypeError, ValueError):
                sizes[key] = 1
        return sizes
    return None


_QPI_NAME = re.compile(r"<Name>([^<]*)</Name>")
#: `[Channel 0.0.0.2]` -- dataset, image, timepoint?, channel: the last number.
_IMARIS_CHANNEL = re.compile(r"\[Channel (?:\d+\.)*(\d+)\][^\[]*?^Name\s*=[ \t]*([^\r\n]*)", re.M)


def embedded_channel_names(tiff, n_channels=None):
    """The channel names `tiff` states about itself, or None.

    Each flavour keeps them somewhere else, and a reader that knows one place
    shows "Channel 1, Channel 2" for the rest:

    - **OME-XML**, any schema version (`ome_xml.channel_names`): `Channel` since
      2009-09, `LogicalChannel`/`ChannelInfo` before.
    - **PerkinElmer / Akoya** (a `.qptiff` scan, inForm's `component_data.tif`):
      every page carries its own XML description, whose first `<Name>` is that
      page's marker ("PDL1 (Opal 520)").
    - **ImageJ**: the stack's slice labels, when it has one per plane.

    A list as long as the file has names for; the caller decides whether that
    is every channel. Reads tags tifffile has already loaded.
    """
    from plexora.server.utils import ome_xml

    try:
        description = tiff.pages[0].tags["ImageDescription"].value
    except Exception:
        description = None
    if isinstance(description, bytes):
        description = description.decode("utf-8", "replace")
    if isinstance(description, str):
        if ome_xml.is_ome_xml(description):
            names = ome_xml.channel_names(description)
            if any(names):
                return names
        elif "PerkinElmer-QPI-ImageDescription" in description[:2048]:
            names = _qpi_names(tiff)
            if names:
                return names
        elif description.lstrip().startswith("[ImarisDataSet]"):
            # Imaris 3/4 "classic": an INI block, one `[Channel N]` section each.
            found = {int(index): name.strip()
                     for index, name in _IMARIS_CHANNEL.findall(description)}
            if found:
                return [found.get(index, "") for index in range(max(found) + 1)]
    labels = (getattr(tiff, "imagej_metadata", None) or {}).get("Labels")
    if isinstance(labels, (list, tuple)) and labels and (n_channels is None or len(labels) == n_channels):
        # ImageJ appends its own notes after a newline ("DAPI\nc:1/4 ...").
        return [str(label).split("\n", 1)[0].strip() for label in labels]
    return None


def _qpi_names(tiff):
    series = tiff.series[0]
    names = []
    for page in (getattr(series, "levels", None) or [series])[0].pages:
        if page is None:
            return None
        found = _QPI_NAME.search(str(getattr(page, "description", "") or ""))
        names.append(found.group(1).strip() if found else "")
    return names if any(names) else None


def _imagej_sizes(tiff):
    """`{SizeC, SizeZ, SizeT}` out of an ImageJ header, or None.

    The second place a file says how its planes are organised, and the only one
    for a stack Fiji wrote and saved as a plain TIFF. The keys are ImageJ's --
    `channels`, `slices`, `frames` -- and each defaults to 1, which is how
    ImageJ itself reads a header that omits them.

    Translated into OME's vocabulary rather than read in ImageJ's, so
    `focal_planes` has one rule to apply instead of two.
    """
    metadata = getattr(tiff, "imagej_metadata", None)
    if not isinstance(metadata, dict):
        return None
    sizes = {}
    for ome_key, imagej_key in (("SizeC", "channels"), ("SizeZ", "slices"),
                                ("SizeT", "frames")):
        try:
            sizes[ome_key] = int(metadata.get(imagej_key, 1) or 1)
        except (TypeError, ValueError):
            sizes[ome_key] = 1
    return sizes


def plane_sizes(tiff):
    """`{SizeC, SizeZ, SizeT}` the file states about itself, or None.

    OME-XML first, an ImageJ header second, nothing third. The order is
    authority: a file carrying both was written by a pipeline that exported
    OME and let ImageJ's own header ride along, and the OME document is the one
    that was written on purpose.
    """
    return _ome_sizes(tiff) or _imagej_sizes(tiff)


def focal_planes(tiff):
    """`(count, middle)` when this file is a single-channel Z-STACK, else `(0, 0)`.

    Deliberately narrow. A stack that carries more than one channel is a real
    hyperstack and the flattening path below is right for it -- an Akoya/CODEX
    export stores its cycles on the Z axis, so `SizeZ > 1` alone says nothing
    -- while a stack of ONE channel is a focus series, and every plane is a
    different attempt at the SAME picture. Only the second one collapses.

    `(0, 0)` for a file that states nothing about its own layout: treating the
    planes as planes is the reading that was there before this rule existed,
    and it is the one that cannot be wrong about a file nobody described.
    """
    sizes = plane_sizes(tiff)
    if not sizes:
        return 0, 0
    if int(sizes.get("SizeC", 1)) != 1 or int(sizes.get("SizeT", 1)) != 1:
        return 0, 0
    depth = int(sizes.get("SizeZ", 1))
    if depth < 2:
        return 0, 0
    return depth, depth // 2


def single_plane_series(tiff, index=0, series=None):
    """One plane of `series`, as a `(1, Y, X)` series **that keeps the pyramid**.

    The pyramid is the whole point. A Xenium focus image is 45450 x 27241 with
    eight SubIFD levels, and a series rebuilt from level 0's pages alone would
    register as a flat single-level image -- every zoomed-out tile decoding a
    gigabyte of JPEG 2000. So the plane is taken out of EVERY level, and the
    resulting series are chained through `levels` exactly the way tifffile
    chains a pyramidal series' own: `aszarr()` then returns a zarr *group*,
    which is the shape `_zarr_level`, `read_tile` and `image_geometry` already
    branch on.

    Falls back to the series it was given whenever the plane is not present at
    some level -- a pyramid that stops early, a frame tifffile could not open
    -- rather than building a chain whose levels disagree about what they hold.
    """
    series = tiff.series[0] if series is None else series
    levels = list(getattr(series, "levels", None) or [series])
    built = []
    for level in levels:
        pages = list(level.pages)
        if index >= len(pages) or pages[index] is None:
            break
        page = pages[index]
        plane = tuple(int(size) for size in page.shape[-2:])
        built.append(tf.TiffPageSeries(
            [page],
            shape=(1,) + plane,
            dtype=series.dtype,
            axes="CYX",
            parent=tiff,
            name=series.name,
        ))
    if not built:
        return series
    base = built[0]
    base.levels = built
    return base


#: On the website, the opened `(TiffFile, channel series)` of the last few
#: files, by (path, size, mtime): what every reader of one file there shares.
#: See `open_channel_series`.
_SHARED = OrderedDict()
_SHARED_LIMIT = 4


def open_channel_series(path):
    """`(tiff, series, shared)`: `path` opened as `tifffile.TiffFile(path,
    is_ome=False)` and its `channel_series`.

    On the website (one thread, `_work.threaded()` false) the pair is shared by
    every reader of the same file as it is now -- the viewer's image provider
    and an agent's `SourceImage` -- because opening it walks every page's
    directory, which for a 40-channel, 7-level OME-TIFF picked from disk is
    1,217 reads through the page and about a second, for an answer identical
    to the one already held. `shared` is then True and the caller must leave
    the file open (the garbage collector closes it once nobody reads it).

    Everywhere else each caller gets a handle of its own (`shared` False), as
    before: tifffile serialises reads of one handle behind a lock, and a
    shared one would put an agent's reads behind the viewer's tiles.
    """
    from plexora import _work

    def opened():
        tiff = ImageTiff(path)
        try:
            return tiff, channel_series(tiff)
        except Exception:
            tiff.close()
            raise

    if _work.threaded():
        return (*opened(), False)
    try:
        stat = os.stat(path)
    except OSError:
        return (*opened(), False)
    key = (os.fspath(path), stat.st_size, stat.st_mtime_ns)
    held = _SHARED.get(key)
    if held is not None and not held[0].filehandle.closed:
        _SHARED.move_to_end(key)
        return (*held, True)
    held = opened()
    _SHARED[key] = held
    while len(_SHARED) > _SHARED_LIMIT:
        _SHARED.popitem(last=False)
    return (*held, True)


def is_shared(tiff) -> bool:
    """Whether `tiff` is one `open_channel_series` shares (and must stay open)."""
    return any(held[0] is tiff for held in list(_SHARED.values()))


def held_ifds(path) -> list[int] | None:
    """The offsets of `path`'s main IFDs, in the order the file chains them,
    from the open the website shares (`open_channel_series`), which walked
    them; None when that open is not held or did not reach the chain's end.
    Reads nothing: the page's reader takes them to read its own open's IFDs
    at once instead of one after another (plexora/browser/node_spec.py
    `ifds`)."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    held = _SHARED.get((os.fspath(path), stat.st_size, stat.st_mtime_ns))
    if held is None or held[0].filehandle.closed:
        return None
    pages = held[0].pages
    # What tifffile walked; `_indexed` once it reached the last IFD's 0.
    offsets = getattr(pages, "_offsets", None)
    if not getattr(pages, "_indexed", False) or not offsets:
        return None
    return [int(offset) for offset in offsets]


def _first_image_planes(tiff, shape) -> int:
    """How many of a `(planes, Y, X)` series' planes are the FIRST image's,
    when the file's OME-XML describes several images of one size; else 0.

    Plexora opens a TIFF without tifffile's OME reader, which groups pages by
    shape: a file holding several scenes or stage positions of one size (a
    bfconvert of a multi-scene .czi or .lif, a ZEN or MetaMorph multi-position
    export) arrives as ONE series with every scene's channels end to end, and
    opened as an image of four times its channels, the first scene's panel
    followed by the other scenes' as if they were more markers. The document
    says where the first image stops.
    """
    from plexora.server.utils import ome_xml

    try:
        description = tiff.pages[0].tags["ImageDescription"].value
    except Exception:
        return 0
    if not isinstance(description, str) or not ome_xml.is_ome_xml(description[:OME_MARKER_WINDOW]):
        return 0
    document = ome_xml.read(description, images=2)
    if document is None or len(document.images) < 2:
        return 0
    first = document.images[0]
    if (first.size_y, first.size_x) != tuple(shape[-2:]) or any(n != 1 for n in first.samples):
        return 0
    planes = first.size_c * first.size_z * first.size_t
    if planes <= 0 or planes >= shape[0] or shape[0] % planes:
        return 0
    return planes


def _leading_pages_series(tiff, series, planes):
    """The first `planes` pages of `series` at every level, as a series that
    keeps the pyramid (`single_plane_series`, for a run of planes)."""
    levels = list(getattr(series, "levels", None) or [series])
    built = []
    for level in levels:
        pages = list(level.pages)[:planes]
        if len(pages) < planes or any(page is None for page in pages):
            return series
        built.append(tf.TiffPageSeries(
            pages,
            shape=(planes,) + tuple(int(size) for size in pages[0].shape[-2:]),
            dtype=series.dtype,
            axes="CYX",
            parent=tiff,
            name=series.name,
        ))
    base = built[0]
    base.levels = built
    return base


def steps_unevenly(series) -> bool:
    """Whether `series` has reduced levels that are NOT each half the one
    before (within a pixel of rounding).

    The viewer addresses level `n` as the image at `1 / 2**n`, and a pyramid
    tifffile hands over is served level for level, which is right for the
    halving pyramids nearly every writer makes. It is wrong for the rest: a
    Leica SCN steps by 4, as does `bfconvert -pyramid-scale 4` and several
    scanners' exports, and their zoomed-out levels were drawn at the wrong
    size. Such a file is read through a halving chain laid over its own
    levels instead (`tiff_region.open_image`), as a remote TIFF and every
    brightfield slide already are.
    """
    levels = list(getattr(series, "levels", None) or [series])
    for finer, coarser in zip(levels, levels[1:]):
        for big, small in zip(finer.shape[-2:], coarser.shape[-2:]):
            if abs(int(big) / 2 - int(small)) > 1:
                return True
    return False


def needs_region_reader(series) -> bool:
    """Whether a channel image must be read through `tiff_region.open_image`
    rather than served from tifffile's own levels: its pyramid does not halve
    (`steps_unevenly`), or its channels are interleaved SAMPLES of one page
    (axes `YXS` that is not a colour picture -- a multispectral or
    grey-plus-alpha export), which read positionally is an image as many
    channels deep as it is tall."""
    return steps_unevenly(series) or str(getattr(series, "axes", "") or "").endswith("S")


def channel_series(tiff):
    """`tiff.series[0]` as a series indexed (channel, y, x).

    Returns the series untouched whenever it already is -- which is every
    OME-TIFF, every plain channel stack, and anything whose axes this cannot
    make sense of. A hyperstack (`TCYX`/`ZCYX`/deeper) comes back with its
    leading axes collapsed into one channel axis; a single-channel Z-stack
    comes back as its middle focal plane; and a lone 2-D plane comes back as a
    single channel rather than as `shape[0]` channels of height `shape[1]`.

    A flattened series carries level 0's pages only. Nothing writes a
    pyramidal hyperstack -- ImageJ has no way to -- and a correct read with no
    pyramid is a better answer than a pyramid over the wrong axes. The two
    single-plane cases DO keep their pyramid; see `single_plane_series`.
    """
    series = tiff.series[0]
    axes = str(getattr(series, "axes", "") or "")
    shape = tuple(int(dimension) for dimension in series.shape)

    # The trailing YX pair is the only thing that makes the leading axes
    # readable as planes. Without it -- an interleaved `YXS` slide, a layout
    # tifffile labelled something unexpected -- there is no reshape that is
    # obviously right, and guessing is worse than the behaviour that is
    # already there. `is_rgb_layout` has taken the interleaved files out of
    # this path well before here anyway.
    if not axes.endswith("YX"):
        return series

    if len(shape) == 3:
        planes = _first_image_planes(tiff, shape)
        if planes:
            series = _leading_pages_series(tiff, series, planes)
            shape = tuple(int(dimension) for dimension in series.shape)
        depth, middle = focal_planes(tiff)
        if depth > 1 and depth == shape[0]:
            # A focus series, not a panel. One channel, in focus.
            return single_plane_series(tiff, middle, series=series)
        # The case every caller already assumes. Returned by identity rather
        # than rebuilt, so a pyramidal OME-TIFF keeps its own `levels` and this
        # module cannot regress the format Plexora is built around.
        return series

    if len(shape) == 2:
        return single_plane_series(tiff, 0, series=series)

    if len(shape) < 2:
        return series

    planes = 1
    for size in shape[:-2]:
        planes *= size

    pages = list(series.pages)
    # A mismatch means the series is not a plain stack of planes after all
    # (a missing page, a shape tifffile derived from metadata rather than
    # from the file). Leave it alone rather than build a series whose pages
    # and shape disagree.
    if planes != len(pages) or any(page is None for page in pages):
        return series

    return tf.TiffPageSeries(
        pages,
        shape=(planes,) + shape[-2:],
        dtype=series.dtype,
        axes="CYX",
        parent=tiff,
        name=series.name,
    )
