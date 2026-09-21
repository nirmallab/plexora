"""Do a project's table, mask and image describe the same sample?

Nothing in any of the three files says so. A quantification table, a
segmentation mask and an image come out of three different steps of a pipeline,
are named by hand, and are attached to a project one at a time -- so pairing a
table with the wrong image, or a mask exported at a different resolution than
the image it was segmented from, is an ordinary mistake that no file format
catches and no error message reports. What it produces instead is a viewer that
works: gates move, cells light up, and the numbers on screen are a different
sample's.

This module is the check nothing else performs. It is core's and not a
plugin's, for the same reason cell opacity is: the question is about the
project, every plugin that draws per-cell results has it, and three plugins
asking it three slightly different ways is three answers to disagree about.

**Findings, not errors.** Everything here is a warning with a sentence
attached, because every one of these states is one a real project is allowed to
be in -- a legitimately cropped region genuinely does cover a corner of its
slide -- and refusing to open a project over a heuristic would be worse than
the mistake it is guarding against. The caller shows them quietly; the user
decides.

**Cheap enough to run on panel open.** Every check below reads the per-column
summary the viewer already computes (`data_model._describe_frame`) plus the
project record. The one exception is the mask's own pixel dimensions, which
cost one metadata-only file open and are cached for the life of the load.
"""

from __future__ import annotations

from typing import Any, Mapping

#: How far past the image edge a coordinate may sit before it is reported.
#: Not zero: a centroid on the last row of pixels rounds to the width itself,
#: and a half-pixel of slop is not a wrong image.
_EDGE_TOLERANCE = 1.005

#: Below this fraction of the image, on BOTH axes, a table is covering a
#: corner rather than the slide.
_CORNER_FRACTION = 0.6

#: How closely the two axes' fractions must agree before the corner is read as
#: one scale factor applied to both -- the signature of coordinates in physical
#: units -- rather than as an arbitrarily shaped crop.
_SAME_SCALE = 0.1

#: How near the origin a corner has to start to count as one.
_ORIGIN_FRACTION = 0.02


def _finding(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _round(value: float) -> str:
    """A number in a sentence: no decimals, and thousands grouped."""
    return f"{round(value):,}"


def _column(description: Mapping[str, Any], name: str | None) -> dict | None:
    """One column's summary, or None when the project never named the column
    or the loaded table no longer holds it."""
    if not name:
        return None
    entry = description.get(name)
    if not isinstance(entry, Mapping) or not entry.get("count"):
        return None
    return entry


def _is_whole(value: float) -> bool:
    return float(value) == int(value)


def _coordinate_findings(x, y, width, height) -> list[dict]:
    """The table against the image it is drawn on.

    Two different mistakes, and they look nothing alike. Coordinates that run
    off the edge are a table paired with the wrong image -- there is no reading
    of the numbers under which a cell sits outside the slide it was measured
    on. Coordinates that all fall in one corner, by the SAME fraction on both
    axes, are the same slide measured in different units: a micron coordinate
    on a 0.325 um/px scan lands at about a third of its pixel address, and does
    so identically in x and y. An arbitrarily shaped crop does not.
    """
    findings = []
    over_x = x["max"] > width * _EDGE_TOLERANCE
    over_y = y["max"] > height * _EDGE_TOLERANCE
    if over_x or over_y:
        axis = "x" if over_x else "y"
        reach = x["max"] if over_x else y["max"]
        extent = width if over_x else height
        side = "wide" if over_x else "tall"
        findings.append(_finding(
            "cells_outside_image",
            f"Cell coordinates run past the image: {axis} reaches "
            f"{_round(reach)}, and the image is {_round(extent)} px {side}. "
            "This table may belong to a different image."))
        return findings

    if x["min"] < -1 or y["min"] < -1:
        findings.append(_finding(
            "cells_outside_image",
            "Some cell coordinates are negative, so those cells fall outside "
            "the image. This table may belong to a different image."))
        return findings

    fraction_x = (x["max"] - x["min"]) / width
    fraction_y = (y["max"] - y["min"]) / height
    widest = max(fraction_x, fraction_y)
    # `widest > 0` keeps a degenerate table -- one cell, or every cell at the
    # same coordinate -- from being reported as covering "the first 0%", which
    # is a true sentence about a different problem.
    if (0 < widest <= _CORNER_FRACTION
            and abs(fraction_x - fraction_y) <= _SAME_SCALE * widest
            and x["min"] <= width * _ORIGIN_FRACTION
            and y["min"] <= height * _ORIGIN_FRACTION):
        findings.append(_finding(
            "cells_in_a_corner",
            f"Cells cover only the first {round(widest * 100)}% of the image, "
            "by the same fraction on both axes. The table's coordinates may be "
            "in microns rather than pixels."))
    return findings


def _cell_id_findings(ids) -> list[dict]:
    """The table against the mask, which is one question: can a row's id name a
    label in the mask?

    A mask's labels are whole numbers from 1 up; 0 is background, in every
    format and every tool that writes one. So an id column holding 0, or
    holding fractions, cannot address the mask however the two were produced --
    and the failure is silent, because an id that matches nothing simply draws
    nothing. Off-by-one id columns are common: several pipelines write the
    row's position rather than the label it was measured from.
    """
    findings = []
    if not (_is_whole(ids["min"]) and _is_whole(ids["max"])):
        findings.append(_finding(
            "ids_not_whole",
            "The cell id column holds fractional values, and a mask's labels "
            "are whole numbers, so gated cells cannot be matched to the mask."))
    elif ids["min"] < 1:
        findings.append(_finding(
            "ids_below_one",
            f"Cell ids start at {_round(ids['min'])}, and a mask's labels "
            "start at 1 -- 0 is background. The ids may be row positions "
            "rather than mask labels."))
    return findings


def _mask_findings(mask_size, width, height) -> list[dict]:
    """The mask against the image.

    The viewer serves mask tiles in the IMAGE's coordinate system -- the mask
    layer is synthesized with the image's width, height and pyramid depth (see
    Project.all_layers) -- so a mask of another size is not rejected anywhere.
    It is drawn, stretched across the wrong pixels, and every outline is in the
    wrong place by a little. That is the hardest of these to see and the one
    most worth saying out loud.
    """
    mask_width, mask_height = mask_size
    if mask_width == width and mask_height == height:
        return []
    return [_finding(
        "mask_size",
        f"The segmentation mask is {_round(mask_width)} x "
        f"{_round(mask_height)} px and the image is {_round(width)} x "
        f"{_round(height)} px. Outlines are drawn in the image's coordinates, "
        "so they will not sit on the cells they came from.")]


def report(project, description: Mapping[str, Any],
           mask_size: tuple | None = None) -> list[dict]:
    """Every mismatch these three inputs can be shown to have, worst first.

    Pure: `project` is a typed record, `description` is the per-column summary
    and `mask_size` is `(width, height)` or None when there is no mask, it is
    on another machine, or its dimensions could not be read. Nothing here opens
    a file, which is what makes the whole set testable against hand-built
    inputs rather than against fixtures on disk.

    An empty list is the ordinary answer and means the checks ran and found
    nothing -- not that they were skipped.
    """
    if not project.has_table:
        return []

    findings: list[dict] = []
    width, height = project.image.width, project.image.height
    roles = project.roles
    x = _column(description, roles.x)
    y = _column(description, roles.y)
    ids = _column(description, roles.cell_id)

    if mask_size and width and height:
        findings.extend(_mask_findings(mask_size, width, height))

    if x and y and width and height:
        findings.extend(_coordinate_findings(x, y, width, height))
    elif roles.x and roles.y and not (x and y):
        # The columns are named but hold nothing the viewer could summarize:
        # every row null, or the image-id subset selected none of them. Either
        # way there is nothing to draw, and a panel that simply stays empty
        # gives no account of why.
        findings.append(_finding(
            "no_cells",
            "The table has no cells with coordinates for this image, so "
            "nothing will be drawn however the threshold is set."))

    if ids and project.segmentation.available:
        findings.extend(_cell_id_findings(ids))

    return findings
