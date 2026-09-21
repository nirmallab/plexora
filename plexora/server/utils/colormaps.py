"""Named colour ramps, as the tile encoders want them.

A DENSITY IS ONE NUMBER PER PIXEL, AND ONE NUMBER PER PIXEL WANTS A RAMP.
Plexora's other coloured tiles are a single hue multiplied by an intensity
(`layer_sources._colourise`), which is the right thing for a fluorescence
channel -- it is what the microscope saw, in the colour the filter was. A
transcript density is not a channel: it is a count, and a count read off a
single-hue ramp is a count read off the bottom third of it, because the eye
resolves lightness far better than saturation. Every heat map in the field is
perceptually uniform for that reason and this is the same list.

Four, deliberately, and the same four the cell explorer offers -- one
perceptually uniform default, one warm, one colour-vision safe, one diverging.
Forty matplotlib colormaps is a menu rather than a choice, and most of them
invent structure that is not in the data. The anchors below are
`cellExplorerColors.js`'s, to the digit, so a density map and a coloured cell
overlay of the same slide are the same colours.

Anchors and not 256 literal rows: a ramp written out in full is three
kilobytes of source nobody can check, and interpolating is exact at every
anchor anyway.
"""

from __future__ import annotations

import numpy as np

#: Stops in an expanded ramp. 256 because the input to it is a uint8 level,
#: so one stop per input value is exactly enough and no lookup ever rounds.
STOPS = 256

RAMPS = {
    "viridis": ("#440154", "#472d7b", "#3b528b", "#2c728e", "#21918c",
                "#28ae80", "#5ec962", "#addc30", "#fde725"),
    "magma": ("#000004", "#1c1044", "#4f127b", "#812581", "#b5367a",
              "#e55964", "#fb8761", "#fec287", "#fcfdbf"),
    "cividis": ("#00224e", "#123570", "#3b496c", "#575d6d", "#707173",
                "#8a8678", "#a59c74", "#c3b369", "#fee838"),
    "coolwarm": ("#3b4cc0", "#6788ee", "#9abbff", "#c9d7f0", "#edd1c2",
                 "#f7a889", "#e26952", "#b40426"),
}

DEFAULT_RAMP = "viridis"


def is_ramp(name) -> bool:
    """Whether this names one of the ramps. Used to tell a colormap request
    apart from the per-colour one, so an unknown name falls back to the
    caller's own colouring rather than silently drawing viridis."""
    return str(name or "").strip().lower() in RAMPS


def _rgb(hexed):
    text = str(hexed).lstrip("#")
    return [int(text[i:i + 2], 16) for i in (0, 2, 4)]


def ramp(name, stops=STOPS) -> np.ndarray:
    """`(stops, 3)` uint8, the ramp from its dark end to its bright one.

    An unknown name gives the default rather than raising: this is reached
    from a query string, and a saved view naming a ramp a later version
    dropped should still draw.
    """
    anchors = RAMPS.get(str(name or "").strip().lower()) or RAMPS[DEFAULT_RAMP]
    points = np.array([_rgb(value) for value in anchors], dtype=np.float32)
    # One interpolation per channel over the anchor positions. `np.interp`
    # rather than a hand-rolled walk: it is exact at the anchors, which is the
    # property that lets the table above be read as the ramp it names.
    where = np.linspace(0.0, 1.0, len(points), dtype=np.float32)
    want = np.linspace(0.0, 1.0, int(stops), dtype=np.float32)
    out = np.empty((int(stops), 3), dtype=np.uint8)
    for channel in range(3):
        out[:, channel] = np.rint(
            np.interp(want, where, points[:, channel])).astype(np.uint8)
    return out


def apply(level8: np.ndarray, name) -> np.ndarray:
    """A uint8 intensity raster as RGB through one ramp.

    A lookup and not arithmetic: 256 rows indexed by the level is one gather
    per pixel, where interpolating per pixel would be three multiplies and a
    branch over a 1024x1024 tile on every pan.
    """
    return ramp(name)[level8]
