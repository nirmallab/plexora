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

A marker with a cell table column is anchored on its cells instead
(`cell_window`): the median in-mask pixel of clearly negative cells to the p90
across clearly positive cells of each one's p95, read from level-0 crops --
the pixels every panel is drawn from. The overview window is then only the
fallback (no table, too few cells, an unreadable mask), and so are the two
rules above: the coarse-level clip and the cell cap are what re-saturate
level-0 pixels.

It is NOT the channel list the sidebar edits (`channelList`), which the user
owns and the sidebar rewrites whole; nothing here changes what a user chose.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np

from plexora.agent.errors import AgentError

#: Bumped when a window's rule changes; a stored record of another version is
#: stale and recomputed.
VERSION = "3"
NAMESPACE = "display"

#: The marker being judged, its reference channels, the nuclear stain.
MARKER_COLOR = "#ffd60a"
REFERENCE_COLORS = ("#22e6e6", "#ff3df2")
NUCLEAR_MUTED_BLUE = "#4f6fae"

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
    """[low, high] for a channel's stats and role; `cap` (a marker's
    `cell_cap`) lowers the top, never below the minimum contrast. A marker's
    `stats["cell_window"]` (`cell_window.window_from_crops`) wins outright."""
    anchored = stats.get("cell_window") if role != "nuclear" else None
    if anchored:
        low = float(anchored["low"])
        high = max(float(anchored["high"]), MIN_CONTRAST * max(low, 1e-6))
        return [low, high if high > low else low + 1.0]
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
        folded = {vocabulary.fold(m): m for m in markers}
        columns = {}
        for name in names:
            column = name if name in markers else folded.get(vocabulary.fold(name))
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


def compute(source, channels, *, nuclear=None, level=None, caps=None,
            cell_windows=None) -> dict:
    """The calibration record for `channels` (names -> keys), JSON-safe.
    `caps` maps a marker channel to its `cell_cap`; `cell_windows` to its
    cell-anchored window (`cell_window.cell_windows`), which wins."""
    from plexora.agent.evidence import cell_window as cellwin

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
        if anchored:
            stats["cell_window"] = dict(anchored)
        window = channel_window(stats, role, cap=cap)
        capped = not anchored and cap is not None and window != channel_window(stats, role)
        flags = []
        if role != "nuclear" and window[1] > THRESHOLDS["wide_ratio"] * max(window[0], 1.0):
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
        out[name] = {"role": role, "key": key, "color": color, "window": window,
                     "window_source": (f"calib:p{NUCLEAR_PERCENTILES[0]:g}-"
                                       f"p{NUCLEAR_PERCENTILES[1]:g}@L{level}"
                                       if role == "nuclear" else
                                       _anchored_source(cellwin) if anchored else
                                       f"calib:p{MARKER_PERCENTILES[0]:g}-"
                                       f"p{MARKER_PERCENTILES[1]:g}@L{level}"
                                       + ("+cellcap" if capped else "")),
                     "stats": stats, "flags": flags}
    return {"version": VERSION, "level": int(level), "channels": out,
            "nuclear": nuclear}


def _anchored_source(cellwin):
    spec = cellwin.CELL_WINDOW
    return (f"{cellwin.SOURCE}:p{spec['negative_pct'][0]:g}-{spec['negative_pct'][1]:g}neg-"
            f"p{spec['positive_pct'][0]:g}-{spec['positive_pct'][1]:g}pos@L{cellwin.LEVEL}")


def _anchors(session, project, keys, nuclear):
    from plexora.agent.evidence import cell_window

    return cell_window.cell_windows(session, project, keys, nuclear=nuclear)


def _table_fingerprint(session, project):
    try:
        return getattr(session.data(project).table, "expression_fingerprint", None)
    except Exception:
        return None


def _store(project):
    from plexora import api

    return api.store(project, NAMESPACE)


def load(project) -> dict | None:
    blob = _store(project).get_state()
    if not blob:
        return None
    try:
        record = json.loads(blob.decode("utf-8"))
    except ValueError:
        return None
    # Another rule's windows: stale, and recomputed by the next calibrate().
    return record if isinstance(record, dict) and record.get("version") == VERSION else None


def revision(project) -> str:
    blob = _store(project).get_state()
    return hashlib.sha1(blob).hexdigest()[:16] if blob else "0"


def save(project, record) -> str:
    blob = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _store(project).put_state(blob)
    return hashlib.sha1(blob).hexdigest()[:16]


def calibrate(session, project, channels=None, *, force=False) -> tuple:
    """(record, changed): compute and store the calibration for a project's
    channels (default every real channel), reusing a stored one unless
    `force` or a channel is missing from it."""
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
    if stored and not force and all(n in (stored.get("channels") or {}) for n in wanted) \
            and stored.get("table") == table:
        return stored, False
    if stored and stored.get("table") != table:
        stored = None       # windows anchored on another matrix's values
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
    merged = dict(stored or {"version": VERSION, "channels": {}})
    merged["table"] = table
    merged.update({k: v for k, v in fresh.items() if k != "channels"})
    merged["channels"] = {**(stored or {}).get("channels", {}), **fresh["channels"]}
    merged["project"] = project
    before = revision(project)
    after = save(project, merged)
    return merged, before != after


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
    if stored and all(n in (stored.get("channels") or {}) for n in channels) \
            and stored.get("table") == table:
        return stored
    record = session.project(project)
    identity = (project, str(record.image.src), tuple(sorted(channels)),
                source_image.ReaderShelf.identity_of(session.image_data(project)),
                _log_transformed(session, project), table)
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
    if len(_CURRENT) >= _CURRENT_LIMIT:
        _CURRENT.pop(next(iter(_CURRENT)))
    _CURRENT[identity] = fresh
    return fresh


def window_for(record, name):
    """(window, source) for a channel from a calibration record, or (None, None)."""
    entry = ((record or {}).get("channels") or {}).get(name)
    if not entry:
        return None, None
    return list(entry["window"]), entry.get("window_source", "calib")


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
    plain = {k: v for k, v in stats.items() if k != "cell_window"}
    return channel_window(plain, entry.get("role", "marker"), cap=stats.get("cell_cap"))


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

    def window(name):
        return window_at(record, name, px_per_px) if px_per_px is not None \
            else channels[name]["window"]

    out = []
    if nuclear and nuclear in channels and nuclear != marker:
        out.append({"name": nuclear, "color": NUCLEAR_MUTED_BLUE,
                    "window": window(nuclear), "enabled": True})
    if marker in channels:
        out.append({"name": marker, "color": MARKER_COLOR,
                    "window": window(marker), "enabled": True})
    for colour, reference in zip(REFERENCE_COLORS, references):
        if reference in channels:
            out.append({"name": reference, "color": colour,
                        "window": window(reference), "enabled": True})
    return out


def needs_vision_check(record) -> list:
    """Channels whose calibration raised a flag worth one look."""
    return sorted(name for name, entry in ((record or {}).get("channels") or {}).items()
                  if entry.get("flags"))
