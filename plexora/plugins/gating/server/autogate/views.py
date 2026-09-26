"""Gating's evidence, assembled: numbers from the table, pictures from the pixels.

The glue between the column statistics here (`profile`, `sampler`,
`bivariate`) and the generic renderers in `plexora.agent.evidence`: which
image channel a marker is, how a stratified sample becomes the rows of a
collage, which cells an overview marks. Shared by the MCP tools and the
gating session, so a tool call and a session packet draw the same thing.
"""

from __future__ import annotations

import numpy as np

from plexora.agent import gate_rule
from plexora.plugins.gating.server.autogate import cells as cellmod
from plexora.plugins.gating.server.autogate import profile as profmod

#: How a T2 collage's three rows are made from the sampler's strata.
T2_ROWS = (
    ("below", "just below the gate", ("just_below", "low_background", "clear_negative")),
    ("near", "at the gate", ("borderline",)),
    ("above", "just above the gate", ("just_above", "moderate_positive", "strong_positive")),
)

STRATA_LABELS = {
    "clear_negative": "clear negative", "low_background": "background",
    "just_below": "just below", "borderline": "borderline", "just_above": "just above",
    "moderate_positive": "moderate +", "strong_positive": "strong +", "extreme": "extreme",
}


def channel_names(ds):
    return [c.get("fullname") or c.get("name") for c in ds.project.image.real_channels]


def image_channel(ds, marker):
    """The image channel a marker column is drawn from, or None."""
    from plexora.ai import vocabulary

    names = channel_names(ds)
    if marker in names:
        return marker
    folded = {vocabulary.fold(n): n for n in names}
    return folded.get(vocabulary.fold(marker))


def image_qc_for(session, ds, marker):
    """The overview QC block for a marker's channel, cached; None without one."""
    channel = image_channel(ds, marker)
    if channel is None or ds.project.image.is_blank:
        return None

    def compute():
        from plexora.agent.evidence import image_qc
        from plexora.agent.presets import nuclear_channel
        from plexora.agent.render import resolve_channel
        from plexora.server.utils import source_image

        records = list(ds.project.image.real_channels)
        _i, found = resolve_channel(channel, records)
        nuclear = nuclear_channel(channel_names(ds))
        nuclear_key = None
        if nuclear:
            _j, nuc = resolve_channel(nuclear, records)
            nuclear_key = source_image.channel_key(nuc)
        with source_image.SHELF.reader(session.image_data(ds.name)) as source:
            if source.is_brightfield:
                return None
            return image_qc.overview_qc(source, source_image.channel_key(found), nuclear_key)

    return ds.cached(("autogate.image_qc", channel), compute)


def full_profile(session, ds, marker, *, seed=0, with_image=True):
    """The marker's profile with the overview QC merged in and re-scored."""
    image = image_qc_for(session, ds, marker) if with_image else None
    profile = profmod.profile_marker(ds, marker, seed=seed, image_qc=image)
    if image_channel(ds, marker) is None:
        profile = dict(profile)
        profile["profile_flags"] = sorted(set(profile.get("profile_flags") or [])
                                          | {"no_image_channel"})
        profile = profmod.score(profile)
    return profile


def call_of(value, low, high):
    return "positive" if bool(gate_rule.passes(np.array([value]), low, high)[0]) \
        else "negative"


def _with_calls(cells, low, high):
    return [dict(c, call=call_of(c["value"], low, high)) for c in cells]


def t2_rows(sample, low, high, per_row=8):
    """Three rows -- below, at and above the gate -- from a stratified sample,
    nearest-the-gate strata first in each."""
    out = []
    for key, label, names in T2_ROWS:
        cells = []
        for name in names:
            cells.extend(sample["strata"].get(name) or [])
        cells = sorted(cells, key=lambda c: abs(c["value"] - low))[:per_row]
        cells = sorted(cells, key=lambda c: c["value"])
        n = sum(sample["counts"].get(name, 0) for name in names)
        out.append({"key": key, "label": f"{label} ({n} cells)",
                    "cells": _with_calls(cells, low, high)})
    return out


def strata_rows(sample, low, high, per_row=6):
    rows = []
    for name, cells in sample["strata"].items():
        rows.append({"key": name, "label": f"{STRATA_LABELS.get(name, name)} "
                                           f"({sample['counts'].get(name, 0)})",
                     "cells": _with_calls(cells[:per_row], low, high)})
    if sample.get("inconsistent"):
        rows.append({"key": "inconsistent", "label": "spatially inconsistent",
                     "cells": _with_calls(sample["inconsistent"][:per_row], low, high)})
    return rows


def flip_rows(delta, per_row=8, labels=None):
    from plexora.agent.evidence.collage import compact_number

    rows = []
    for index, interval in enumerate(delta["intervals"]):
        name = (labels or {}).get(index) or \
            f"{compact_number(interval['from'])} to {compact_number(interval['to'])}"
        rows.append({"key": f"i{index + 1}",
                     "label": f"i{index + 1}: {name} ({interval['n_flip']} cells flip)",
                     "cells": [dict(c, caption=compact_number(c["value"])) for c in
                               interval["cells"][:per_row]]})
    return rows


def positive_points(ds, marker, low, high=None):
    """(xs, ys) of every positive cell with a position."""
    c = cellmod.cells(ds)
    v = cellmod.values(ds, marker)
    high = float(np.nanmax(v)) if high is None else float(high)
    positive = gate_rule.passes(v, low, high) & c.valid
    return c.xs[positive], c.ys[positive]


def image_payload(rendered):
    """One `_images` entry for a render: WebP (or PNG) bytes with the format."""
    return {"data": rendered["image"], "format": rendered["format"]}
