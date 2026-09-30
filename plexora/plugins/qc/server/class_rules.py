"""What a confirmed artifact class may claim, and what its region does to cells.

Two rules, both pure functions of names the result already holds:

`supported_class` -- a class is only kept when the evidence it needs exists.
Autofluorescence is tissue that glows on its own, in every channel it is
excited in, so it can be told from a stain's off-target signal only by an
autofluorescence (blank, unstained) channel or by the same structures being
bright in at least `AF_MIN_MARKERS` markers at once. Without either, the
class falls back to what the detector itself measured when that was compact
bright objects (`COMPACT_CLASSES`: specks, a blob, a saturated plateau -- so
the outline is still traced round them), and otherwise to that marker's
background, `excessive_background`. The change is recorded, never silently
made.

`region_level` -- whether a region fails whole cells or flags one channel's
values. A class that damages the tissue (`schemas.PHYSICAL_CLASSES`), a
region scoped to every channel, and a region that reaches the nuclear stain
the cells were segmented from, all make the cell unreadable: level "cell".
Anything scoped to some channels is level "marker", for those channels only.
"""

from __future__ import annotations

import re

from plexora.plugins.qc.server import schemas

#: [cal] markers (not nuclear stains) the same structures must be bright in for
#: a region to be called autofluorescence without an autofluorescence channel.
AF_MIN_MARKERS = 2

#: A channel acquired to show autofluorescence: named for it, or blank /
#: unstained / background. "AF488"-style dye names are NOT matched -- only a
#: name that is "AF" (optionally numbered) on its own.
_AF_WORDS = re.compile(r"autofluor|\bblank\b|\bunstained\b|\bempty\b|\bbackground\b|"
                       r"^af\s?\d{0,2}$|^bg\s?\d{0,2}$")


def _words(name) -> str:
    return re.sub(r"[_\-.:/]+", " ", str(name or "")).strip().lower()


def is_af_channel(name) -> bool:
    return bool(_AF_WORDS.search(_words(name)))


def _is_nuclear(name) -> bool:
    from plexora.plugins.qc.server.cycles import is_nuclear

    return bool(is_nuclear(name))


#: What a detector's hint may fall back to: classes of compact bright objects.
COMPACT_CLASSES = ("antibody_aggregate", "debris_or_foreign_object", "saturation_or_clipping")


def supported_class(klass, *, channels, image_channels, hint=None):
    """(class, adjustment | None). `channels` are the region's (the channels
    it was confirmed in); `image_channels` every channel of the image; `hint`
    the class the detector proposed."""
    if klass != "autofluorescence":
        return klass, None
    af = [c for c in image_channels or () if is_af_channel(c)]
    markers = [c for c in dict.fromkeys(channels or ()) if c and not _is_nuclear(c)
               and not is_af_channel(c)]
    if af or len(markers) >= AF_MIN_MARKERS:
        return klass, None
    to = hint if hint in COMPACT_CLASSES else "excessive_background"
    why = (f"autofluorescence needs an autofluorescence channel or the same structures "
           f"bright in {AF_MIN_MARKERS} markers; this image has no such channel and the "
           f"region is in {', '.join(markers) or 'no marker'} only")
    return to, {"from": klass, "to": to, "why": why}


def segmentation_channel(ds, cycles=None):
    """The nuclear stain the cells were most likely segmented from: the first
    nuclear column of the table (what the counterstain module reads)."""
    from plexora.plugins.qc.server.cells import modules

    try:
        first, _last = modules._nuclear_columns(ds, {"cycles": cycles or []})
    except Exception:  # no table
        return None
    return first


def region_level(klass, scope, channels, *, segmentation=None):
    """("cell", []) or ("marker", [channel, ...]) for one live region."""
    channels = [c for c in dict.fromkeys(channels or ()) if c]
    if klass in schemas.PHYSICAL_CLASSES or scope == "all_channels" or not channels:
        return "cell", []
    if segmentation and _same(segmentation, channels):
        return "cell", []
    return "marker", channels


def _same(name, names):
    folded = str(name).casefold()
    return any(str(n).casefold() == folded for n in names)


def table_column(name, markers):
    """The table column a channel's values are in: the same name, else the
    same name ignoring case; None when the table does not measure it."""
    if name in markers:
        return name
    folded = str(name).casefold()
    return next((m for m in markers if str(m).casefold() == folded), None)
