"""Hand-written TIFFs for the brightfield tests.

Written with tifffile directly rather than through any convenience wrapper, for
the same reason `ngff_fixtures.py` writes NGFF metadata by hand: the point of
most of these is a storage layout a well-behaved writer would not produce --
interleaved samples with no OME block, three `minisblack` planes that mean red,
green and blue, an Aperio description on a file called `.svs`. Detection is
about exactly those, so a fixture built by a library that normalises them would
test the library.

Every image here is small (a few hundred pixels) and every one is *legible* at
the level the tests check: light background with a dark patch for brightfield,
dark background with a bright patch for fluorescence. A fixture full of noise
would pass the structural tiers and say nothing about the pixel tier.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import tifffile as tf

#: Background of a transmitted-light image: mostly slide, and bright.
_LIGHT = 236
#: Background of a fluorescence image: mostly nothing, and dark.
_DARK = 4


def _stained(height, width, dtype=np.uint8):
    """(y, x, 3) -- a pale field with one eosin-and-haematoxylin patch in it."""
    image = np.full((height, width, 3), _LIGHT, dtype=dtype)
    image[height // 4:height // 2, width // 4:width // 2] = (150, 92, 172)
    image[height // 2:height * 3 // 4, width // 3:width * 2 // 3] = (214, 138, 156)
    return image


def _emitted(channels, height, width, dtype=np.uint16):
    """(c, y, x) -- a dark field with one bright blob per channel."""
    ceiling = np.iinfo(dtype).max
    image = np.full((channels, height, width), _DARK, dtype=dtype)
    for index in range(channels):
        top = (index + 1) * height // (channels + 2)
        left = (index + 1) * width // (channels + 2)
        image[index, top:top + height // 8, left:left + width // 8] = ceiling // 2
    return image


def write_rgb_ome_tiff(path, height=512, width=640, pyramid=0):
    """An H&E scan exported as an interleaved RGB OME-TIFF.

    The case the whole detector exists for: three samples that are colour, in a
    container Plexora had only ever seen fluorescence in. tifffile writes the
    OME block itself, and for `photometric='rgb'` that block is one `Channel`
    with `SamplesPerPixel="3"` -- which is the canonical OME spelling of RGB and
    the tier-three rule.

    `pyramid` extra halvings are written as SubIFDs when asked for.
    """
    path = Path(path)
    image = _stained(height, width)
    if not pyramid:
        tf.imwrite(path, image, photometric="rgb", metadata={"axes": "YXS"})
        return path
    levels = [image]
    for _ in range(pyramid):
        previous = levels[-1]
        levels.append(previous[::2, ::2])
    with tf.TiffWriter(path) as writer:
        writer.write(levels[0], photometric="rgb", subifds=pyramid,
                     metadata={"axes": "YXS"})
        for level in levels[1:]:
            writer.write(level, photometric="rgb", subfiletype=1)
    return path


def write_interleaved_tiff(path, height=512, width=640):
    """A plain colour TIFF with no OME block at all.

    Tier two on its own: `photometric=RGB` plus three samples in one plane, the
    same statement Bio-Formats' `isRGB()` makes.
    """
    path = Path(path)
    tf.imwrite(path, _stained(height, width), photometric="rgb",
               description="")
    return path


def write_planar_rgb_tiff(path, height=512, width=640):
    """Colour written as three SEPARATE planes.

    Rare, legal, and the reason `is_rgb_layout` does not test planar
    configuration: this is as much an RGB image as an interleaved one, and a
    reader that required interleaving would serve it as three grey channels.
    """
    path = Path(path)
    image = np.ascontiguousarray(np.moveaxis(_stained(height, width), -1, 0))
    tf.imwrite(path, image, photometric="rgb", planarconfig="separate")
    return path


def write_planar_fluorescence(path, channels=3, height=512, width=640,
                              names=("DAPI", "CD3", "Ki67"), dtype=np.uint16):
    """A genuine multiplex panel with as many channels as an RGB image has
    samples -- the fixture that proves nothing decides on channel count."""
    path = Path(path)
    tf.imwrite(path, _emitted(channels, height, width, dtype),
               photometric="minisblack",
               metadata={"axes": "CYX",
                         "Channel": {"Name": list(names[:channels])}})
    return path


def write_ambiguous_planar(path, light, height=512, width=640, dtype=np.uint8):
    """Three `minisblack` 8-bit planes that declare nothing.

    The genuinely ambiguous case, and the one the override exists for: the file
    says only "three planes", so the pixel tier is all there is. `light=True`
    makes it look like transmitted light, `light=False` like emitted.
    """
    path = Path(path)
    if light:
        image = np.ascontiguousarray(
            np.moveaxis(_stained(height, width, dtype), -1, 0))
    else:
        image = _emitted(3, height, width, dtype)
    tf.imwrite(path, image, photometric="minisblack", metadata={"axes": "CYX"})
    return path


def write_grayscale(path, height=512, width=640, light=True):
    """One bright 8-bit plane. Bright, and still not brightfield -- there is no
    colour in it to read."""
    path = Path(path)
    value = _LIGHT if light else _DARK
    tf.imwrite(path, np.full((height, width), value, np.uint8),
               photometric="minisblack")
    return path


#: An Aperio ImageDescription, trimmed to the fields the reader looks at. The
#: `|MPP = ...|` field is the only place an SVS states its pixel size.
APERIO_DESCRIPTION = (
    "Aperio Image Library v11.2.1\n"
    "{width}x{height} [0,0 {width}x{height}] (240x240) JPEG/RGB Q=70"
    "|AppMag = 40|MPP = 0.2465|ScanScope ID = TEST"
)


def write_svs_like(path, height=1024, width=1280, levels=3):
    """A pyramidal RGB TIFF at a `.svs` path, with an Aperio description.

    Not a real Aperio file -- tifffile writes ordinary SubIFDs rather than the
    separate-IFD layout a scanner produces -- but it is the same thing the
    reader has to cope with: colour samples, a pyramid whose steps are the
    file's own business, and the scale hidden in a pipe-delimited string.
    """
    path = Path(path)
    levels_data = [_stained(height, width)]
    for _ in range(levels - 1):
        # Quartering, like Aperio, so the virtual halving chain has a gap in it
        # and the level picker has to resample rather than pass through.
        levels_data.append(levels_data[-1][::4, ::4])
    description = APERIO_DESCRIPTION.format(width=width, height=height)
    with tf.TiffWriter(path) as writer:
        writer.write(levels_data[0], photometric="rgb", subifds=levels - 1,
                     description=description)
        for level in levels_data[1:]:
            writer.write(level, photometric="rgb", subfiletype=1)
    return path


def write_ome_with_contrast(path, method, channels=3, height=256, width=320):
    """A planar stack whose OME block states its `ContrastMethod`.

    The highest-precision signal a file can carry, and the one that has to beat
    the storage layout in both directions -- so this writes it onto a layout
    that would otherwise be read the other way.
    """
    path = Path(path)
    image = _emitted(channels, height, width)
    channel_xml = "".join(
        f'<Channel ID="Channel:0:{index}" SamplesPerPixel="1" '
        f'ContrastMethod="{method}"/>'
        for index in range(channels))
    description = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYCZT" '
        f'Type="uint16" SizeX="{width}" SizeY="{height}" SizeC="{channels}" '
        f'SizeZ="1" SizeT="1">{channel_xml}</Pixels></Image></OME>')
    tf.imwrite(path, image, photometric="minisblack", description=description)
    return path
