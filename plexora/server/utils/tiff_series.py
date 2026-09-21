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

from xml.etree import ElementTree

import tifffile as tf


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
