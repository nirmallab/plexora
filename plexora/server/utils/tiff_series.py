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

Masks are **not** routed through this. A label image is a single 2-D plane and
`read_tile` indexes it with two subscripts, so presenting one as `(1, Y, X)`
would break the reader that is currently correct.
"""

import tifffile as tf


def channel_series(tiff):
    """`tiff.series[0]` as a series indexed (channel, y, x).

    Returns the series untouched whenever it already is -- which is every
    OME-TIFF, every plain channel stack, and anything whose axes this cannot
    make sense of. A hyperstack (`TCYX`/`ZCYX`/deeper) comes back with its
    leading axes collapsed into one channel axis, and a lone 2-D plane comes
    back as a single channel rather than as `shape[0]` channels of height
    `shape[1]`.

    A flattened series carries level 0's pages only. Nothing writes a
    pyramidal hyperstack -- ImageJ has no way to -- and a correct read with no
    pyramid is a better answer than a pyramid over the wrong axes.
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

    # The case every caller already assumes. Returned by identity rather than
    # rebuilt, so a pyramidal OME-TIFF keeps its own `levels` and this module
    # cannot regress the format Plexora is built around.
    if len(shape) == 3:
        return series

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
