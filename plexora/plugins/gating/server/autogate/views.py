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
    """The image channel a marker column is drawn from, or None
    (`vocabulary.match_name`: `CD45` is drawn from `03_CD45_ArgoFluor520`)."""
    from plexora.ai import vocabulary

    return vocabulary.match_name(marker, channel_names(ds))


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


def full_profile(session, ds, marker, *, seed=0, with_image=True, compartment=None):
    """The marker's profile with the overview QC merged in and re-scored.
    `compartment` (the panel's) decides whether a DNA correlation is bleed."""
    image = image_qc_for(session, ds, marker) if with_image else None
    profile = profmod.profile_marker(ds, marker, seed=seed, image_qc=image,
                                     compartment=compartment)
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


def _per_row(layout):
    from plexora.agent.evidence.collage import LAYOUTS

    return LAYOUTS[layout]["per_row"]


def t2_rows(sample, low, high, per_row=None, spatial=False):
    """Three rows -- below, at and above the gate -- from a stratified sample,
    nearest-the-gate strata first in each; as many cells a row as the layout
    draws. `spatial` with a sample the sampler spread over environments
    (`sample["spatial"]["select"]`): `t2_rows_spatial`."""
    if spatial and (sample.get("spatial") or {}).get("select"):
        return t2_rows_spatial(sample, low, high, per_row)
    per_row = per_row or _per_row("t2")
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


def strata_rows(sample, low, high, per_row=None):
    per_row = per_row or _per_row("strata")
    rows = []
    for name, cells in sample["strata"].items():
        rows.append({"key": name, "label": f"{STRATA_LABELS.get(name, name)} "
                                           f"({sample['counts'].get(name, 0)})",
                     "cells": _with_calls(cells[:per_row], low, high)})
    if sample.get("inconsistent"):
        rows.append({"key": "inconsistent", "label": "spatially inconsistent",
                     "cells": _with_calls(sample["inconsistent"][:per_row], low, high)})
    return rows


def flip_rows(delta, per_row=None, labels=None, spread=False):
    """One row per candidate interval. `spread` (spatial switch on) keeps
    cells evenly spaced over the interval's sampled range rather than the
    first `per_row`, which drops its brightest end."""
    from plexora.agent.evidence.collage import compact_number

    per_row = per_row or _per_row("flips")
    rows = []
    for index, interval in enumerate(delta["intervals"]):
        name = (labels or {}).get(index) or \
            f"{compact_number(interval['from'])} to {compact_number(interval['to'])}"
        cells = interval["cells"]
        if spread and len(cells) > per_row:
            keep = sorted(set(np.linspace(0, len(cells) - 1, per_row).round().astype(int)))
            cells = [cells[i] for i in keep]
        rows.append({"key": f"i{index + 1}",
                     "label": f"i{index + 1}: {name} ({interval['n_flip']} cells flip)",
                     "cells": [dict(c, caption=compact_number(c["value"])) for c in
                               cells[:per_row]]})
    return rows


def positive_points(ds, marker, low, high=None, within=None):
    """(xs, ys) of every positive cell with a position -- only among the
    positives of partner gate `within` (`{marker, gate}`) when given."""
    c = cellmod.cells(ds)
    v = cellmod.values(ds, marker)
    high = float(np.nanmax(v)) if high is None else float(high)
    positive = gate_rule.passes(v, low, high) & c.valid
    if within:
        w = cellmod.values(ds, within["marker"])
        positive &= gate_rule.passes(w, float(within["gate"]), float(np.nanmax(w)))
    return c.xs[positive], c.ys[positive]


def positive_ids_in(ds, marker, low, high, bounds, within=None, limit=None):
    """Ids of the positive cells inside `bounds` (`{x, y, width, height}`,
    full-resolution px), among a partner's positives when `within` is given --
    what a conditional gate's field outlines."""
    from plexora.agent.limits import MAX_IDS

    c = cellmod.cells(ds)
    v = cellmod.values(ds, marker)
    high = float(np.nanmax(v)) if high is None else float(high)
    keep = gate_rule.passes(v, low, high) & c.valid
    if within:
        w = cellmod.values(ds, within["marker"])
        keep &= gate_rule.passes(w, float(within["gate"]), float(np.nanmax(w)))
    x0, y0 = float(bounds["x"]), float(bounds["y"])
    keep &= (c.xs >= x0) & (c.xs < x0 + float(bounds["width"])) \
        & (c.ys >= y0) & (c.ys < y0 + float(bounds["height"]))
    return [int(i) for i in c.ids[keep][:int(limit or MAX_IDS)]]


def image_payload(rendered):
    """One `_images` entry for a render: WebP (or PNG) bytes with the format."""
    return {"data": rendered["image"], "format": rendered["format"]}


# -- rows that compare environments (`SessionOptions.spatial` stratify and decide) -----

#: The extra row a spatial first look draws: cells at the gate with a bright
#: neighbour touching them -- the lowest weak positives and the highest
#: negatives beside one (`sampler.PRIORITY_GROUPS`).
NEAR_BRIGHT = ("near_bright", "at the gate, beside a bright neighbour")

#: What a like-for-like row is called on the collage (`sampler.CONTRAST_ROWS`).
CONTRAST_LABELS = {
    "bright": "beside a bright neighbour", "clear": "no bright neighbour, sparse tissue",
    "dense_clear": "no bright neighbour, dense tissue", "low_snr": "high local background",
    "reference": "strong positives (reference)", "background": "clear negatives, high "
                                                               "background",
}


def spatial_caption(cell, low, high):
    """`value` and the call (+/-), then `·n` when a bright neighbour touches
    the cell, else `·b` in high local background: at most eight characters,
    stored in the manifest so a redraw is the same picture."""
    from plexora.agent.evidence.collage import compact_number

    call = cell.get("call") or call_of(cell["value"], low, high)
    text = compact_number(cell["value"]) + ("+" if call == "positive" else "-")
    if cell.get("spill") not in (None, "none"):
        text += "·n"
    elif cell.get("snr_class") == "low":
        text += "·b"
    return text[:8]


def _env_counts(cells):
    out = {}
    for cell in cells:
        env = cell.get("env")
        if env:
            out[env] = out.get(env, 0) + 1
    return out


def _spatial_cells(cells, low, high):
    return [dict(c, call=call_of(c["value"], low, high),
                 caption=spatial_caption(c, low, high)) for c in cells]


def t2_rows_spatial(sample, low, high, per_row=None):
    """`t2_rows` over a spatial sample: each core row spread over the
    environments (`sampler.allocate`) rather than cut to the cells nearest
    the gate, and a fourth row, `near_bright`, when at least
    `min_env_row_cells` cells sit at the gate beside a bright neighbour.
    `below` / `near` / `above` keep their meaning (the first look's rule
    table reads only them)."""
    from plexora.plugins.gating.server.autogate import sampler
    from plexora.plugins.gating.server.autogate.schemas import SPATIAL_RULES

    per_row = per_row or _per_row("t2")
    priority = sample.get("priority") or {}
    weak = sorted(priority.get("weak_pos_bright") or [], key=lambda c: c["value"])[:per_row // 2]
    beside = sorted(priority.get("neg_beside_strong") or [],
                    key=lambda c: -c["value"])[:per_row - len(weak)]
    bright = sorted(weak + beside, key=lambda c: c["value"])
    taken = {c["cell_id"] for c in bright}
    out = []
    for key, label, names in T2_ROWS:
        pool = [c for name in names for c in sample["strata"].get(name) or []
                if c["cell_id"] not in taken]
        by_env = {}
        for cell in pool:
            by_env.setdefault(cell.get("env") or "", []).append(cell)
        take = sampler.allocate(per_row, {e: len(v) for e, v in by_env.items()})
        cells = []
        for env, members in by_env.items():
            members = sorted(members, key=lambda c: abs(c["value"] - low))
            cells.extend(members[:take.get(env, 0)])
        cells = sorted(cells, key=lambda c: c["value"])
        n = sum(sample["counts"].get(name, 0) for name in names)
        out.append({"key": key, "label": f"{label} ({n} cells)",
                    "cells": _spatial_cells(cells, low, high), "envs": _env_counts(cells),
                    "limits": (sample.get("limits") or {}).get(names[0])})
    if len(bright) >= int(SPATIAL_RULES["min_env_row_cells"]):
        counts = sample.get("priority_counts") or {}
        n = int(counts.get("weak_pos_bright", 0)) + int(counts.get("neg_beside_strong", 0))
        out.append({"key": NEAR_BRIGHT[0], "label": f"{NEAR_BRIGHT[1]} ({n} cells)",
                    "cells": _spatial_cells(bright, low, high), "envs": _env_counts(bright)})
    return out


def contrast_rows(contrast, low, high, per_row=None):
    """The like-for-like look's rows (`sampler.contrast_cells`), in
    `sampler.CONTRAST_ROWS` order; absent rows are left out."""
    from plexora.plugins.gating.server.autogate import sampler

    per_row = per_row or _per_row("t2")
    out = []
    for key in sampler.CONTRAST_ROWS:
        cells = (contrast.get("rows") or {}).get(key)
        if not cells:
            continue
        cells = sorted(cells, key=lambda c: c["value"])[:per_row]
        n = (contrast.get("counts") or {}).get(key, len(cells))
        out.append({"key": key, "label": f"{CONTRAST_LABELS[key]} ({n} cells)",
                    "cells": _spatial_cells(cells, low, high), "envs": _env_counts(cells)})
    return out


def rows_env(rows) -> dict:
    """{row: {env: cells shown}} -- what a look is told of where each row's
    cells come from."""
    return {r["key"]: r.get("envs") or {} for r in rows if r.get("envs")}
