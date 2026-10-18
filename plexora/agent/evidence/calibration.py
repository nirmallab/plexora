"""One display calibration per project: what "a sensible window" is, decided once.

How bright to draw a channel is a function of its pixels, not a question for a
model -- and it has to be the SAME function everywhere, or a headless render
and the open viewer show one gate two ways. So the windows are computed here,
deterministically, from the overview level (`image_qc.read_overview`), and
stored in the project's own store under the `display` namespace. Both
consumers read the record:

- `plexora.agent.render` resolves a channel's `"auto"` window from it;
- a whole-tissue panel draws coarse pixels, so it takes `window_at` its
  scale rather than the level-0 window (QC's sheets);
- mirroring an agent's work into an open tab applies `as_viewer_channels()`,
  so the tab shows those very windows and colours.

The rule is the viewer's own immediate-display hint -- p50 to p99.5 of the
non-zero overview pixels (`data_model._HINT_PERCENTILES`) -- with the top held
at least 3x the bottom so a dim channel stays readable, and the nuclear stain
drawn p30-p99 in a desaturated blue, dark enough that yellow and magenta
overlays read over it.

A marker's top is also capped from the cell side: CELL_CAP_FACTOR times the
brightest cells' mean intensity (the cell table's CELL_CAP_PERCENTILE, in the
image's units). A few bright specks -- debris, a fold, a hot pixel cluster --
otherwise put the overview's p99.5 an order of magnitude above every cell, and
every cell draws black. A window still wider than `wide_ratio` is flagged.

A marker with a cell table column is drawn from its own cells instead
(`cell_window`, read in a disc around each cell's centroid in level-0 crops --
the pixels every panel is drawn from -- never from a cell QC left out), on an
asinh curve (`display_curve`) fixed by two display levels:

- black at the negatives' low percentile, under the background, so the
  background shows as a faint floor -- the limit is visible, nothing is
  clipped to black that is tissue;
- the background's brightest pixels (its noise p99) at NOISE_LEVEL: dim;
- the bright cells' brightest structure (the titre) at TITRE_LEVEL: bright,
  with room above it, so a ring, a nucleus or granules keep their shape.

When a linear window already meets both, the curve is linear. A marker with
no bright cells is drawn as if its titre were TITRE_FLOOR_GAPS background
gaps above black, so its noise is never stretched to white. The rule's
window is column c3 of a brightness ladder (`columns_for`, GAINS); a vision
check may pick another column per marker (`record_choices`), and that choice
outlives a recomputation. The overview window is then only the fallback (no
table, too few cells), and so are the two rules above: the coarse-level clip
and the cell cap are what re-saturate level-0 pixels. The record carries the
QC fingerprint it was anchored under, so a QC change recomputes it.

The viewer's canvas is offered a little more (`view_spec`): a marker
VIEW_GAIN brighter than the evidence draws it, and the nuclear stain on its
own nuclei (`nuclear_view`) rather than the overview window -- dimmer than
the markers, a backdrop they read over.

It is NOT the channel list the sidebar edits (`channelList`), which the user
owns and the sidebar rewrites whole; nothing here changes what a user chose.
"""

from __future__ import annotations

import hashlib
import json
import threading

import numpy as np

from plexora.agent.errors import AgentError

#: Bumped when a window's rule changes; a stored record of another version is
#: stale and recomputed. 6: the nuclear stain is measured on its own cells
#: (`nuclear_view`).
VERSION = "6"
NAMESPACE = "display"

#: [cal] the display levels (0 black .. 1 white) a marker's curve is solved
#: for: the background's noise p99 draws dim, the titre bright with headroom.
NOISE_LEVEL = 0.12
TITRE_LEVEL = 0.85
#: [cal] a marker without bright cells is drawn as if its titre were this
#: many background gaps (noise p99 - black) above black.
TITRE_FLOOR_GAPS = 6.0
#: [cal] the brightness ladder a vision check chooses from: every display
#: level times the gain (`gain_white`). c3 is the rule's own window.
GAINS = (0.64, 0.8, 1.0, 1.25, 1.6)
COLUMN_IDS = ("c1", "c2", "c3", "c4", "c5")
RULE_COLUMN = "c3"
#: [cal] a column is never chosen whose negative tile's p90 draws above
#: `negative_level`, whose bright cells have more than `clipped_share` of
#: their disc at white, or whose bright cells' p99 draws below `bright_level`.
VETOES = {"negative_level": 0.35, "clipped_share": 0.05, "bright_level": 0.3}
#: [cal] the viewer draws a marker this much brighter than the evidence does
#: (every display level times it, black kept: `view_spec`): the column a
#: check chose is judged on crops, and a canvas reads a little dark at it.
VIEW_GAIN = 1.1
#: The intensity scale every picture is drawn on by default -- a reader's
#: evidence, the contrast check, the mirrored viewer and the viewer's canvas:
#: "linear", or "log" (the calibrated asinh curve). Linear since 2026-10-18:
#: on lsp35766_p193 the asinh curve lifted the stroma's haze to the level of
#: CD8a's weak rings and the nuclei (drawn at their overview p99) washed the
#: tiles out; drawn as the canvas draws Linear, the same cells read as their
#: own stain and the walk went from 8.00 to 7.69 (the user's gate 7.3). The
#: record keeps both scales (`window`/`curve` and `linear_window`), so the
#: viewer's Log brightness switch still offers Log (`view_spec`).
SCALE = "linear"
#: [cal] the display levels the viewer draws the nuclear stain at
#: (`nuclear_view`): a typical nucleus's brightest structure, and the bright
#: nuclei's -- under a marker's TITRE_LEVEL x VIEW_GAIN, so the nuclei are a
#: backdrop the markers read over.
NUCLEAR_LEVELS = {"typical": 0.5, "titre": 0.75}

#: The marker being judged, its reference channels, the nuclear stain.
MARKER_COLOR = "#ffd60a"
REFERENCE_COLORS = ("#22e6e6", "#ff3df2")
NUCLEAR_MUTED_BLUE = "#4f6fae"
#: Pixels at full white in a contrast check (`display_grid`): saturation
#: drawn as such, never mistaken for stain.
CLIP_COLOR = "#ff2a2a"

MARKER_PERCENTILES = (50.0, 99.5)
NUCLEAR_PERCENTILES = (30.0, 99.0)
MIN_CONTRAST = 3.0

#: [cal] the cell-side cap on a marker window's top.
CELL_CAP_PERCENTILE = 99.5
CELL_CAP_FACTOR = 1.5
#: Fewer cells than this with a value: no cap.
CELL_CAP_MIN_CELLS = 100

#: [cal] flags that ask for a look before the window is trusted.
THRESHOLDS = {"saturated": 0.005, "dim_ratio": 4.0, "dim_decades": 0.6,
              "high_background": 2.0, "nuclear_uneven": 12.0, "wide_ratio": 50.0}


def _stats(plane):
    nonzero = plane[plane > 0]
    data = nonzero if nonzero.size >= 100 else plane.ravel()
    if not data.size:
        return {k: 0.0 for k in ("p01", "p30", "p50", "p99", "p995", "p999", "max")}
    keys = ("p01", "p30", "p50", "p99", "p995", "p999")
    values = np.percentile(data, [1, 30, 50, 99, 99.5, 99.9])
    out = {k: float(v) for k, v in zip(keys, values)}
    out["max"] = float(data.max())
    return out


def channel_window(stats, role, cap=None):
    """[low, high] for a channel's overview stats and role; `cap` (a marker's
    `cell_cap`) lowers the top, never below the minimum contrast."""
    if role == "nuclear":
        low, high = stats["p30"], stats["p99"]
    else:
        low, high = stats["p50"], stats["p995"]
        high = max(high, MIN_CONTRAST * max(low, 1e-6))
        # A cap below the window's own bottom means the table is not in the
        # image's units (scaled, normalised): it says nothing about pixels.
        if cap is not None and cap > low:
            high = min(high, max(cap, MIN_CONTRAST * max(low, 1e-6)))
    high = min(high, max(stats["max"], low + 1.0)) if stats["max"] > low else low + 1.0
    if not high > low:
        high = low + 1.0
    return [float(low), float(high)]


def cell_cap(values, log_transformed):
    """The cell-side cap for a marker column, in the image's units, or None
    when the column cannot give one (too few values, or negative values: a
    z-scored or arcsinh table is not in intensity units)."""
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if values.size < CELL_CAP_MIN_CELLS or values.min() < 0:
        return None
    top = float(np.percentile(values, CELL_CAP_PERCENTILE))
    if log_transformed:
        top = float(np.expm1(top))
    return top * CELL_CAP_FACTOR


def _cell_caps(session, project, names) -> dict:
    """{channel: cell_cap} for the channels with a table column of the same
    marker (exact name, else the vocabulary's folding); {} when the project
    has no table to read."""
    from plexora.ai import vocabulary

    try:
        ds = session.data(project)
        markers = list(ds.table.markers)
        columns = {}
        for name in names:
            column = vocabulary.match_name(name, markers)
            if column is not None:
                columns[name] = column
        if not columns:
            return {}
        values = ds.table.columns(sorted(set(columns.values())))
        caps = {name: cell_cap(values[column], ds.table.log_transformed)
                for name, column in columns.items()}
        return {k: v for k, v in caps.items() if v is not None}
    except Exception:  # a cap is a refinement; a window without one is still a window
        return {}


def _log_transformed(session, project):
    try:
        return bool(session.data(project).table.log_transformed)
    except Exception:
        return None


def _gap(measured):
    """The background's spread above black (noise p99 - black), never 0."""
    black = float(measured["black"])
    gap = float(measured["noise_p99"]) - black
    if gap > 0:
        return gap
    titre = measured.get("titre")
    if titre is not None and float(titre) > black:
        return (float(titre) - black) / 50.0
    return max(abs(float(measured["background"]) - black), 1.0)


def rule_from(measured) -> dict:
    """{window, curve, floored} for a marker's measurements (`cell_window.
    measure`, or its `low_band`): black at `black`, the noise p99 at
    NOISE_LEVEL, the titre (floored) at TITRE_LEVEL."""
    from plexora.server.utils import display_curve

    black = float(measured["black"])
    gap = _gap(measured)
    titre = measured.get("titre")
    titre_gap = float(titre) - black if titre is not None else 0.0
    floor = TITRE_FLOOR_GAPS * gap
    floored = titre_gap < floor
    curve, white_gap = display_curve.solve(gap, max(titre_gap, floor), NOISE_LEVEL,
                                           TITRE_LEVEL)
    return {"window": [black, black + float(white_gap)], "curve": curve,
            "floored": bool(floored)}


def gain_white(window, curve, gain):
    """The white point that draws every level of (`window`, `curve`) `gain`
    times as bright (black and cofactor kept)."""
    import math

    from plexora.server.utils import display_curve

    black, white = float(window[0]), float(window[1])
    curve = display_curve.normalise(curve)
    if curve is None:
        return black + (white - black) / float(gain)
    c = curve["cofactor"]
    return black + c * math.sinh(math.asinh((white - black) / c) / float(gain))


def columns_for(rule) -> list:
    """The brightness ladder over a rule: [{id, gain, white}], c3 the rule."""
    return [{"id": cid, "gain": gain, "white": gain_white(rule["window"], rule["curve"], gain)}
            for cid, gain in zip(COLUMN_IDS, GAINS)]


def linear_window(entry) -> list:
    """A marker's window for a Linear display (the viewer's Log brightness
    off): the rule's black, the titre (floored as the rule floors it) at
    TITRE_LEVEL, and a vision choice's gain over that. An entry without a cell
    rule (the nuclear stain, an overview window) is already linear."""
    rule, anchors = entry.get("rule"), entry.get("anchors")
    if not rule or not anchors or "noise_p99" not in anchors:
        return [float(v) for v in entry["window"]]
    choice = entry.get("choice") or {}
    band = entry.get("rule_low") if choice.get("band") == "low" and entry.get("rule_low") \
        else rule
    black = float(band["window"][0])
    titre = anchors.get("titre")
    gap = _gap({**anchors, "black": float(rule["window"][0])})
    titre_gap = float(titre) - black if titre is not None else 0.0
    white = black + max(titre_gap, TITRE_FLOOR_GAPS * gap) / TITRE_LEVEL
    return [black, gain_white([black, white], None, float(choice.get("gain") or 1.0))]


def drawn_entry(entry):
    """`entry` as a picture draws it on the default scale (`SCALE`). Linear: a
    cell-calibrated marker at its linear window (a vision choice's gain
    included, `linear_window`), the nuclear stain at its view's linear window
    -- a dim backdrop the markers read over, never its overview p99 -- and no
    curve. Log: as stored. Every evidence window is read through this
    (`window_for`, `display_spec`); `view_spec` offers the viewer both."""
    if not entry or SCALE != "linear":
        return entry
    if entry.get("role") == "nuclear":
        window = (entry.get("view") or {}).get("linear_window") or entry["window"]
    else:
        window = linear_window(entry)
    return {**entry, "window": [float(v) for v in window], "curve": None}


def linear_columns(entry) -> list:
    """The brightness ladder over a marker's linear window, any choice left
    out: [{id, gain, white}], c3 the rule's own. A column picked from it is a
    gain `linear_window` applies, as `columns_for`'s is over the Log rule."""
    base = linear_window({**entry, "choice": None})
    return [{"id": cid, "gain": gain, "white": gain_white(base, None, gain)}
            for cid, gain in zip(COLUMN_IDS, GAINS)]


def entry_curve(entry):
    """A record entry's curve (`display_curve`), None for linear."""
    from plexora.server.utils import display_curve

    return display_curve.normalise((entry or {}).get("curve"))


def apply_choice(entry, choice) -> dict:
    """The entry with a choice (`record_choices`) applied: the chosen
    column's gain over the chosen band's rule. A choice for a column the
    entry has no rule for leaves it as it is."""
    if not choice or not entry.get("rule"):
        return entry
    rule = entry.get("rule_low") if choice.get("band") == "low" and entry.get("rule_low")         else entry["rule"]
    gain = float(choice.get("gain") or 1.0)
    window = [float(rule["window"][0]), gain_white(rule["window"], rule["curve"], gain)]
    return {**entry, "window": window, "curve": rule["curve"],
            "chosen_by": choice.get("chosen_by", "vision"), "choice": dict(choice)}


def chosen_on_scale(record, name) -> bool:
    """Whether a check chose `name`'s column on the ladder of the scale
    pictures are drawn on now (`SCALE`); a choice stored before choices said
    was made on Log's."""
    choice = ((record or {}).get("choices") or {}).get(name)
    return bool(choice) and choice.get("scale", "log") == SCALE


def needs_choice(record, name) -> bool:
    """True for a marker whose window a vision check should look at: drawn by
    the cell rule, with bright cells to show, and not chosen yet on the scale
    pictures are drawn on (a Log-ladder choice is asked again on Linear's). A
    dim or floored marker is asked too: it is the hardest to draw, and the
    floor is only the rule's guess at keeping its noise off white."""
    entry = ((record or {}).get("channels") or {}).get(name) or {}
    if entry.get("role") != "marker" or not entry.get("rule") or not entry.get("columns"):
        return False
    if chosen_on_scale(record, name):
        return False
    return bool((entry.get("cells") or {}).get("bright"))


def nuclear_view(measured) -> dict | None:
    """{window, curve, linear_window, chosen_by, source, anchors}: how the
    viewer draws the nuclear stain, from its own cells (`cell_window.
    measure_nuclear`) -- black in the gaps between nuclei, a typical
    nucleus's brightest structure at NUCLEAR_LEVELS["typical"] and the bright
    nuclei's at NUCLEAR_LEVELS["titre"] (Linear: the bright nuclei's alone).
    None when the measurements cannot place it."""
    from plexora.agent.evidence import cell_window as cellwin
    from plexora.server.utils import display_curve

    black = float(measured["black"])
    typical = float(measured["typical"]) - black
    if not typical > 0:
        return None
    low_at, high_at = NUCLEAR_LEVELS["typical"], NUCLEAR_LEVELS["titre"]
    titre = measured.get("titre")
    titre_gap = float(titre) - black if titre is not None else 0.0
    if titre_gap > typical:
        curve, white_gap = display_curve.solve(typical, titre_gap, low_at, high_at)
        linear_gap = titre_gap / high_at
    else:
        curve, white_gap = None, typical / low_at
        linear_gap = white_gap
    return {"window": [black, black + float(white_gap)], "curve": curve,
            "linear_window": [black, black + float(linear_gap)],
            "chosen_by": "rule:nuclear",
            "source": f"{cellwin.SOURCE}:nuclear:t{low_at:g}-b{high_at:g}@L{cellwin.LEVEL}",
            "anchors": {"black_source": "cells",
                        **{k: measured[k] for k in ("black", "typical", "titre", "n_typical",
                                                     "n_bright", "radius_px", "black_source")
                           if k in measured}}}


def view_spec(entry) -> dict | None:
    """{window, curve, linear_window, chosen_by} a channel is offered to the
    viewer at, or None (the viewer's own levelling stands):

    - a marker anchored on its cells: its window -- a check's choice, or the
      rule's -- VIEW_GAIN brighter, black kept, in Log and in Linear;
    - the nuclear stain: its `view` (`nuclear_view`).

    The evidence (crops, sheets, an agent's look mirrored into the tab) keeps
    the entry's own window: this is what a person's canvas settles on."""
    from plexora.server.utils import display_curve

    entry = entry or {}
    if entry.get("role") == "nuclear":
        view = entry.get("view")
        if not view:
            return None
        return {"window": [float(v) for v in view["window"]],
                "curve": display_curve.normalise(view.get("curve")),
                "linear_window": [float(v) for v in view["linear_window"]],
                "chosen_by": view.get("chosen_by", "rule:nuclear")}
    if entry.get("role") != "marker" or not entry.get("rule"):
        return None
    window = [float(v) for v in entry["window"]]
    curve = entry_curve(entry)
    linear = linear_window(entry)
    return {"window": [window[0], gain_white(window, curve, VIEW_GAIN)], "curve": curve,
            "linear_window": [float(linear[0]), gain_white(linear, None, VIEW_GAIN)],
            "chosen_by": entry.get("chosen_by", "rule")}


def pick_column(columns, negatives_clear, pattern_clear, *, levels=None,
                has_low_band=False) -> dict:
    """{column, gain, band, chosen_by} from a vision check's two lists of
    column ids (where the negatives look like background, and where the
    marker's pattern shows in the bright cells without washing out).

    Columns the numbers veto (`levels` {id: {negative_level, clipped_share,
    bright_level}} against VETOES) are dropped first. Then, nearest the rule's
    own column (ties: the darker):
    - in both lists: `vision`;
    - the negatives clear nowhere: the background band is stained (a marker
      on most cells): the low band's rule (`vision:reanchored`), or the
      darkest column when there is none (`vision:dark`);
    - the pattern seen nowhere: the rule (`rule:pattern_not_seen`);
    - otherwise the lists do not meet: nearest in the negatives' list
      (`vision:conflict`)."""
    gains = {c["id"]: float(c["gain"]) for c in columns}
    ids = [c["id"] for c in columns]
    vetoed = set()
    for cid, level in (levels or {}).items():
        if level.get("negative_level", 0.0) > VETOES["negative_level"] \
                or level.get("clipped_share", 0.0) > VETOES["clipped_share"] \
                or level.get("bright_level", 1.0) < VETOES["bright_level"]:
            vetoed.add(cid)
    allowed = [cid for cid in ids if cid not in vetoed]
    rule_at = ids.index(RULE_COLUMN) if RULE_COLUMN in ids else len(ids) // 2

    def nearest(candidates):
        return min(candidates, key=lambda cid: (abs(ids.index(cid) - rule_at), gains[cid]))

    def out(cid, chosen_by, band="negatives"):
        return {"column": cid, "gain": gains.get(cid, 1.0), "band": band,
                "chosen_by": chosen_by, "vetoed": sorted(vetoed)}

    if not allowed:
        return out(RULE_COLUMN, "rule:vetoed")
    clear = [cid for cid in allowed if cid in set(negatives_clear or ())]
    seen = [cid for cid in allowed if cid in set(pattern_clear or ())]
    both = [cid for cid in clear if cid in seen]
    if both:
        return out(nearest(both), "vision")
    if not clear:
        if has_low_band:
            return out(RULE_COLUMN, "vision:reanchored", band="low")
        return out(min(allowed, key=lambda cid: gains[cid]), "vision:dark")
    if not seen:
        return out(RULE_COLUMN, "rule:pattern_not_seen")
    return out(nearest(clear), "vision:conflict")


def compute(source, channels, *, nuclear=None, level=None, caps=None,
            cell_windows=None) -> dict:
    """The calibration record for `channels` (names -> keys), JSON-safe.
    `caps` maps a marker channel to its `cell_cap`; `cell_windows` to its
    cell measurements (`cell_window.cell_windows`), whose rule wins."""
    from plexora.agent.evidence import image_qc

    level = image_qc.overview_level(source) if level is None else level
    nuclear_plane = None
    if nuclear is not None:
        nuclear_plane, _l, _c = image_qc.read_overview(source, channels[nuclear], level)
    tissue = image_qc.tissue_mask(nuclear_plane)
    out = {}
    for name, key in channels.items():
        plane, _l, ceiling = image_qc.read_overview(source, key, level)
        role = "nuclear" if name == nuclear else "marker"
        stats = _stats(plane)
        stats["saturation_fraction"] = float((plane >= 0.98 * ceiling).mean()) \
            if plane.size else 0.0
        cap = (caps or {}).get(name) if role != "nuclear" else None
        if cap is not None:
            stats["cell_cap"] = float(cap)
        anchored = (cell_windows or {}).get(name) if role != "nuclear" else None
        rule = rule_from(anchored) if anchored else None
        window = list(rule["window"]) if rule else channel_window(stats, role, cap=cap)
        capped = not anchored and cap is not None and window != channel_window(stats, role)
        flags = []
        if role != "nuclear" and not anchored \
                and window[1] > THRESHOLDS["wide_ratio"] * max(window[0], 1.0):
            flags.append("wide_window")
        if stats["saturation_fraction"] > THRESHOLDS["saturated"]:
            flags.append("saturated")
        decades = float(np.log10((stats["p995"] + 1) / (stats["p50"] + 1)))
        if stats["p995"] < THRESHOLDS["dim_ratio"] * max(stats["p50"], 1e-6) \
                or decades < THRESHOLDS["dim_decades"]:
            if role != "nuclear":
                flags.append("dim")
        if tissue is not None and tissue.sum() > 100 and (~tissue).sum() > 100:
            on, off = float(np.median(plane[tissue])), float(np.median(plane[~tissue]))
            stats["p50_tissue"], stats["p50_off_tissue"] = on, off
            if role != "nuclear" and on < THRESHOLDS["high_background"] * max(off, 1.0):
                flags.append("high_background")
            if role == "nuclear":
                lo, hi = np.percentile(plane[tissue], [10, 90])
                if hi > THRESHOLDS["nuclear_uneven"] * max(lo, 1.0):
                    flags.append("nuclear_uneven")
        if stats["p999"] <= stats["p50"] * 1.05:
            flags.append("empty")
        color = NUCLEAR_MUTED_BLUE if role == "nuclear" else MARKER_COLOR
        entry = {"role": role, "key": key, "color": color, "window": window,
                 "window_source": (f"calib:p{NUCLEAR_PERCENTILES[0]:g}-"
                                   f"p{NUCLEAR_PERCENTILES[1]:g}@L{level}"
                                   if role == "nuclear" else
                                   _anchored_source(anchored, rule) if anchored else
                                   f"calib:p{MARKER_PERCENTILES[0]:g}-"
                                   f"p{MARKER_PERCENTILES[1]:g}@L{level}"
                                   + ("+cellcap" if capped else "")),
                 "stats": stats, "flags": flags}
        if anchored:
            entry.update({
                "curve": rule["curve"], "rule": rule, "columns": columns_for(rule),
                "chosen_by": "rule",
                "anchors": {k: anchored[k] for k in (
                    "black", "background", "noise_p99", "titre", "band", "reanchored",
                    "n_negative", "n_bright", "radius_px") if k in anchored},
                "cells": anchored.get("cells") or {}})
            if anchored.get("low_band") and not anchored.get("reanchored"):
                entry["rule_low"] = rule_from(anchored["low_band"])
            if anchored.get("reanchored"):
                flags.append("reanchored_low")
        elif role == "nuclear" and (cell_windows or {}).get(name):
            # Its window above stays the evidence's; the viewer's is its cells'.
            measured = cell_windows[name]
            view = nuclear_view(measured)
            if view is None and stats.get("p30") is not None:
                # Discs that lie inside their nuclei hold no gap to put black
                # in: the overview's background instead.
                view = nuclear_view({**measured, "black_source": "overview",
                                     "black": min(float(measured["black"]), stats["p30"])})
            if view:
                entry["view"] = view
        out[name] = entry
    return {"version": VERSION, "level": int(level), "channels": out,
            "nuclear": nuclear}


def _anchored_source(anchored, rule):
    from plexora.agent.evidence import cell_window as cellwin

    spec = cellwin.CELL_WINDOW
    band = spec["low_pct"] if anchored.get("reanchored") else spec["negative_pct"]
    kind = "asinh" if rule.get("curve") else "linear"
    return (f"{cellwin.SOURCE}:p{band[0]:g}-{band[1]:g}neg:{kind}"
            f":n{NOISE_LEVEL:g}-t{TITRE_LEVEL:g}@L{cellwin.LEVEL}")


def apply_choices(record) -> dict:
    """`record` with its stored `choices` applied to the entries they name."""
    choices = (record or {}).get("choices") or {}
    if not choices:
        return record
    channels = dict(record.get("channels") or {})
    for name, choice in choices.items():
        if name in channels:
            channels[name] = apply_choice(channels[name], choice)
    return {**record, "channels": channels}


def _anchors(session, project, keys, nuclear):
    from plexora.agent.evidence import cell_window

    return cell_window.cell_windows(session, project, keys, nuclear=nuclear)


def _table_fingerprint(session, project):
    try:
        return getattr(session.data(project).table, "expression_fingerprint", None)
    except Exception:
        return None


def _qc_fingerprint(session, project):
    """The QC exclusions the anchors were read under (`cell_exclusions`): a
    window anchored on cells QC has since left out is stale."""
    from plexora.agent import cell_exclusions

    try:
        return cell_exclusions.fingerprint(cell_exclusions.current(session.data(project)))
    except Exception:
        return None


def _store(project):
    from plexora import api

    return api.store(project, NAMESPACE)


def _load_raw(project) -> dict | None:
    blob = _store(project).get_state()
    if not blob:
        return None
    try:
        record = json.loads(blob.decode("utf-8"))
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def load(project) -> dict | None:
    record = _load_raw(project)
    # Another rule's windows: stale, and recomputed by the next calibrate().
    return record if record and record.get("version") == VERSION else None


def load_current(session, project) -> dict | None:
    """The stored record when it was anchored on the table and the QC
    exclusions as they are now; None when there is none or it is stale."""
    record = load(project)
    if record is None:
        return None
    if record.get("table") != _table_fingerprint(session, project) \
            or record.get("qc") != _qc_fingerprint(session, project):
        return None
    return record


def revision(project) -> str:
    blob = _store(project).get_state()
    return hashlib.sha1(blob).hexdigest()[:16] if blob else "0"


def save(project, record) -> str:
    blob = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _store(project).put_state(blob)
    return hashlib.sha1(blob).hexdigest()[:16]


def calibrate(session, project, channels=None, *, force=False, reset_choices=False) -> tuple:
    """(record, changed): compute and store the calibration for a project's
    channels (default every real channel), reusing a stored one unless
    `force` or a channel is missing from it. A vision check's choices are
    kept (re-applied over the fresh rule) unless `reset_choices`."""
    from plexora.agent.presets import nuclear_channel
    from plexora.agent.render import resolve_channel
    from plexora.server.utils import source_image

    record = session.project(project)
    if record.image.is_blank:
        raise AgentError("unsupported_modality", "this sample has no image to calibrate")
    channel_records = list(record.image.real_channels)
    names = [c.get("fullname") or c.get("name") for c in channel_records]
    wanted = list(channels) if channels else names
    stored = load(project)
    table = _table_fingerprint(session, project)
    qc = _qc_fingerprint(session, project)
    if stored and not force and all(n in (stored.get("channels") or {}) for n in wanted) \
            and stored.get("table") == table and stored.get("qc") == qc:
        return stored, False
    # A vision check's choices outlive a recomputation: they are a gain over
    # whatever the rule now says, not a window.
    choices = {} if reset_choices else dict((stored or {}).get("choices") or {})
    if stored and (stored.get("table") != table or stored.get("qc") != qc):
        stored = None       # windows anchored on another matrix's values, or other cells
    keys = {}
    for name in wanted:
        _index, found = resolve_channel(name, channel_records)
        keys[found.get("fullname") or found.get("name")] = source_image.channel_key(found)
    nuclear = nuclear_channel(names)
    if nuclear and nuclear not in keys:
        _index, found = resolve_channel(nuclear, channel_records)
        keys[nuclear] = source_image.channel_key(found)
    image_data = session.image_data(project)
    nuclear_name = nuclear if nuclear in keys else None
    # Read before the source is opened here: the anchors read crops through
    # the same reader shelf, which does not nest.
    anchors = _anchors(session, project, keys, nuclear_name)
    caps = _cell_caps(session, project, keys)
    with source_image.SHELF.reader(image_data) as source:
        if source.is_brightfield:
            raise AgentError("unsupported_modality",
                             "a brightfield image has no per-channel windows to calibrate")
        fresh = compute(source, keys, nuclear=nuclear_name, caps=caps, cell_windows=anchors)
    with _lock(project):
        # The computation above ran unlocked (it reads crops for seconds); a
        # choice recorded meanwhile (`record_choices`) is newer than the one
        # this started from, and is kept.
        latest = load(project)
        if latest and not reset_choices:
            choices = {**choices, **(latest.get("choices") or {})}
        merged = dict(stored or {"version": VERSION, "channels": {}})
        merged["table"] = table
        merged["qc"] = qc
        merged.update({k: v for k, v in fresh.items() if k != "channels"})
        merged["channels"] = {**(stored or {}).get("channels", {}), **fresh["channels"]}
        merged["project"] = project
        if choices:
            merged["choices"] = choices
        else:
            merged.pop("choices", None)
        merged = apply_choices(merged)
        before = revision(project)
        after = save(project, merged)
    return merged, before != after


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict = {}


def _lock(project):
    """One writer of a project's record at a time (`calibrate`'s save and
    `record_choices`): a session's bulk pass and the viewer's first-open
    calibration may both be at it."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(project), threading.Lock())


def record_choices(project, choices) -> tuple:
    """(record, revision): store a vision check's choices ({channel: choice},
    `pick_column` plus who and when) and apply them. Channels the record does
    not hold are ignored; None when there is no current record."""
    with _lock(project):
        return _record_choices(project, choices)


def _record_choices(project, choices):
    record = load(project)
    if record is None:
        return None, revision(project)
    # Each choice says which ladder it was picked on (`chosen_on_scale`).
    known = {k: {**v, "scale": v.get("scale", SCALE)} for k, v in (choices or {}).items()
             if k in (record.get("channels") or {})}
    record = {**record, "choices": {**(record.get("choices") or {}), **known}}
    record = apply_choices(record)
    return record, save(project, record)


def forget_choices(project, channels=None) -> tuple:
    """(record, forgotten): an earlier contrast check's choices taken off --
    every channel's, or `channels'` -- their windows back to the rule's, so
    the next check asks about them again (an auto-contrast run's `redo`).
    (None, []) when there is no current record."""
    with _lock(project):
        record = load(project)
        if record is None:
            return None, []
        choices = dict(record.get("choices") or {})
        names = list(choices) if channels is None else [c for c in channels if c in choices]
        if not names:
            return record, []
        entries = dict(record.get("channels") or {})
        for name in names:
            choices.pop(name, None)
            entry = entries.get(name)
            if entry and entry.get("rule"):
                rule = entry["rule"]
                entry = {k: v for k, v in entry.items() if k != "choice"}
                entries[name] = {**entry, "window": [float(v) for v in rule["window"]],
                                 "curve": rule["curve"], "chosen_by": "rule"}
        record = {**record, "channels": entries, "choices": choices}
        save(project, record)
        return record, names


_CURRENT: dict = {}
_CURRENT_LIMIT = 16


def current(session, project, channels) -> dict:
    """The calibration to draw with: the stored one when it covers `channels`,
    otherwise one computed now and kept in memory only -- a read never writes
    the project's store (`calibrate_display` does, with a receipt)."""
    from plexora.agent.render import resolve_channel
    from plexora.agent.presets import nuclear_channel
    from plexora.server.utils import source_image

    stored = load(project)
    table = _table_fingerprint(session, project)
    qc = _qc_fingerprint(session, project)
    if stored and all(n in (stored.get("channels") or {}) for n in channels) \
            and stored.get("table") == table and stored.get("qc") == qc:
        return stored
    record = session.project(project)
    identity = (project, str(record.image.src), tuple(sorted(channels)),
                source_image.ReaderShelf.identity_of(session.image_data(project)),
                _log_transformed(session, project), table, qc)
    if identity in _CURRENT:
        return _CURRENT[identity]
    channel_records = list(record.image.real_channels)
    names = [c.get("fullname") or c.get("name") for c in channel_records]
    keys = {}
    for name in channels:
        _index, found = resolve_channel(name, channel_records)
        keys[found.get("fullname") or found.get("name")] = source_image.channel_key(found)
    nuclear = nuclear_channel(names)
    nuclear_name = nuclear if nuclear in keys else None
    anchors = _anchors(session, project, keys, nuclear_name)
    caps = _cell_caps(session, project, keys)
    with source_image.SHELF.reader(session.image_data(project)) as source:
        fresh = compute(source, keys, nuclear=nuclear_name, caps=caps, cell_windows=anchors)
    fresh["table"] = table
    fresh["qc"] = qc
    if (stored or {}).get("choices"):
        fresh = apply_choices({**fresh, "choices": stored["choices"]})
    if len(_CURRENT) >= _CURRENT_LIMIT:
        _CURRENT.pop(next(iter(_CURRENT)))
    _CURRENT[identity] = fresh
    return fresh


def window_for(record, name):
    """(window, source) for a channel from a calibration record, or (None, None)."""
    entry = drawn_entry(((record or {}).get("channels") or {}).get(name))
    if not entry:
        return None, None
    return list(entry["window"]), entry.get("window_source", "calib")


def display_spec(record, name, px_per_px=None) -> dict | None:
    """{window, curve, source} for a channel drawn at `px_per_px` full-
    resolution pixels per output pixel (`window_at`); the asinh cofactor
    scales with the window there, so the curve keeps its shape. None when the
    record has no entry for it."""
    from plexora.server.utils import display_curve

    entry = drawn_entry(((record or {}).get("channels") or {}).get(name))
    if not entry:
        return None
    base = [float(v) for v in entry["window"]]
    window = window_at(record, name, px_per_px) if px_per_px is not None else list(base)
    curve = entry_curve(entry)
    if curve is not None and window != base and base[1] > base[0]:
        curve = display_curve.scaled(curve, (window[1] - window[0]) / (base[1] - base[0]))
    return {"window": window, "curve": curve,
            "source": entry.get("window_source", "calib")}


def viewer_curve(curve) -> dict:
    """A curve as the viewer's channel commands take it: kind always named."""
    from plexora.server.utils import display_curve

    curve = display_curve.normalise(curve)
    return dict(curve) if curve else {"kind": "linear"}


#: [cal] Full-resolution pixels per drawn pixel between which a marker's
#: cell-anchored window (read from level-0 crops) gives way to its overview
#: window (the overview level's own p50-p99.5). A pixel drawn from a coarse
#: level averages a bright cell with its surroundings, so the level-0 top is
#: several times anything a whole-tissue panel holds and the panel draws
#: black; between the two the window moves geometrically.
SCALE_BLEND = (2.0, 16.0)


def overview_window(record, name):
    """A channel's window for pixels drawn from the overview level: the stats'
    percentile rule with the cell cap, never the cell anchor. None when the
    record holds no stats for it."""
    entry = ((record or {}).get("channels") or {}).get(name)
    stats = (entry or {}).get("stats")
    if not stats or "p50" not in stats:
        return None
    return channel_window(stats, entry.get("role", "marker"), cap=stats.get("cell_cap"))


def window_at(record, name, px_per_px):
    """A channel's window for a panel drawing `px_per_px` full-resolution
    pixels per output pixel (`SCALE_BLEND`); None when the record has no
    window for it. Deterministic, so a panel stays a pure function of it."""
    window, _source = window_for(record, name)
    if window is None:
        return None
    overview = overview_window(record, name)
    if overview is None or px_per_px is None:
        return window
    return blend_windows(window, overview, px_per_px)


def blend_windows(window, overview, px_per_px):
    """`window` (level 0) moved toward `overview` as the drawn scale coarsens:
    the one at `SCALE_BLEND[0]` and finer, the other at `SCALE_BLEND[1]` and
    coarser, geometric in between."""
    if list(overview) == list(window):
        return list(window)
    fine, coarse = SCALE_BLEND
    t = float(np.clip(np.log(max(float(px_per_px), 1e-6) / fine) / np.log(coarse / fine),
                      0.0, 1.0))
    if t == 0.0:
        return list(window)
    if t == 1.0:
        return list(overview)
    low, high = (float(np.exp((1 - t) * np.log(max(a, 1e-6)) + t * np.log(max(b, 1e-6))))
                 for a, b in zip(window, overview))
    return [low, high if high > low else low + 1.0]


def as_viewer_channels(record, marker, references=(), nuclear=None, px_per_px=None):
    """The `viewer_set_channels` list that shows a marker the way the evidence
    did: nuclear in muted blue, the marker in yellow, references in cyan and
    magenta, each at its calibrated window -- at the scale the tab is shown
    (`window_at`) when `px_per_px` is given."""
    channels = (record or {}).get("channels") or {}
    nuclear = nuclear or (record or {}).get("nuclear")

    def shown(name, colour):
        spec = display_spec(record, name, px_per_px)
        return {"name": name, "color": colour, "window": spec["window"],
                "curve": viewer_curve(spec["curve"]), "enabled": True}

    out = []
    if nuclear and nuclear in channels and nuclear != marker:
        out.append(shown(nuclear, NUCLEAR_MUTED_BLUE))
    if marker in channels:
        out.append(shown(marker, MARKER_COLOR))
    for colour, reference in zip(REFERENCE_COLORS, references):
        if reference in channels:
            out.append(shown(reference, colour))
    return out


def needs_vision_check(record) -> list:
    """Channels whose calibration raised a flag worth one look."""
    return sorted(name for name, entry in ((record or {}).get("channels") or {}).items()
                  if entry.get("flags"))
