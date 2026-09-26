"""One display calibration per project: what "a sensible window" is, decided once.

How bright to draw a channel is a function of its pixels, not a question for a
model -- and it has to be the SAME function everywhere, or a headless render
and the open viewer show one gate two ways. So the windows are computed here,
deterministically, from the overview level (`image_qc.read_overview`), and
stored in the project's own store under the `display` namespace. Both
consumers read the record:

- `plexora.agent.render` resolves a channel's `"auto"` window from it;
- mirroring an agent's work into an open tab applies `as_viewer_channels()`,
  so the tab shows those very windows and colours.

The rule is the viewer's own immediate-display hint -- p50 to p99.5 of the
non-zero overview pixels (`data_model._HINT_PERCENTILES`) -- with the top held
at least 3x the bottom so a dim channel stays readable, and the nuclear stain
drawn p30-p99 in a desaturated blue, dark enough that yellow and magenta
overlays read over it.

It is NOT the channel list the sidebar edits (`channelList`), which the user
owns and the sidebar rewrites whole; nothing here changes what a user chose.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np

from plexora.agent.errors import AgentError

VERSION = "1"
NAMESPACE = "display"

#: The marker being judged, its reference channels, the nuclear stain.
MARKER_COLOR = "#ffd60a"
REFERENCE_COLORS = ("#22e6e6", "#ff3df2")
NUCLEAR_MUTED_BLUE = "#4f6fae"

MARKER_PERCENTILES = (50.0, 99.5)
NUCLEAR_PERCENTILES = (30.0, 99.0)
MIN_CONTRAST = 3.0

#: [cal] flags that ask for a look before the window is trusted.
THRESHOLDS = {"saturated": 0.005, "dim_ratio": 4.0, "dim_decades": 0.6,
              "high_background": 2.0, "nuclear_uneven": 12.0}


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


def channel_window(stats, role):
    """[low, high] for a channel's stats and role."""
    if role == "nuclear":
        low, high = stats["p30"], stats["p99"]
    else:
        low, high = stats["p50"], stats["p995"]
        high = max(high, MIN_CONTRAST * max(low, 1e-6))
    high = min(high, max(stats["max"], low + 1.0)) if stats["max"] > low else low + 1.0
    if not high > low:
        high = low + 1.0
    return [float(low), float(high)]


def compute(source, channels, *, nuclear=None, level=None) -> dict:
    """The calibration record for `channels` (names -> keys), JSON-safe."""
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
        window = channel_window(stats, role)
        flags = []
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
                                       f"calib:p{MARKER_PERCENTILES[0]:g}-"
                                       f"p{MARKER_PERCENTILES[1]:g}@L{level}"),
                     "stats": stats, "flags": flags}
    return {"version": VERSION, "level": int(level), "channels": out,
            "nuclear": nuclear}


def _store(project):
    from plexora import api

    return api.store(project, NAMESPACE)


def load(project) -> dict | None:
    blob = _store(project).get_state()
    if not blob:
        return None
    try:
        return json.loads(blob.decode("utf-8"))
    except ValueError:
        return None


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
    if stored and not force and all(n in (stored.get("channels") or {}) for n in wanted):
        return stored, False
    keys = {}
    for name in wanted:
        _index, found = resolve_channel(name, channel_records)
        keys[found.get("fullname") or found.get("name")] = source_image.channel_key(found)
    nuclear = nuclear_channel(names)
    if nuclear and nuclear not in keys:
        _index, found = resolve_channel(nuclear, channel_records)
        keys[nuclear] = source_image.channel_key(found)
    image_data = session.image_data(project)
    with source_image.SHELF.reader(image_data) as source:
        if source.is_brightfield:
            raise AgentError("unsupported_modality",
                             "a brightfield image has no per-channel windows to calibrate")
        fresh = compute(source, keys, nuclear=nuclear if nuclear in keys else None)
    merged = dict(stored or {"version": VERSION, "channels": {}})
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
    if stored and all(n in (stored.get("channels") or {}) for n in channels):
        return stored
    record = session.project(project)
    identity = (project, str(record.image.src), tuple(sorted(channels)),
                source_image.ReaderShelf.identity_of(session.image_data(project)))
    if identity in _CURRENT:
        return _CURRENT[identity]
    channel_records = list(record.image.real_channels)
    names = [c.get("fullname") or c.get("name") for c in channel_records]
    keys = {}
    for name in channels:
        _index, found = resolve_channel(name, channel_records)
        keys[found.get("fullname") or found.get("name")] = source_image.channel_key(found)
    nuclear = nuclear_channel(names)
    with source_image.SHELF.reader(session.image_data(project)) as source:
        fresh = compute(source, keys, nuclear=nuclear if nuclear in keys else None)
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


def as_viewer_channels(record, marker, references=(), nuclear=None):
    """The `viewer_set_channels` list that shows a marker the way the evidence
    did: nuclear in muted blue, the marker in yellow, references in cyan and
    magenta, each at its calibrated window."""
    channels = (record or {}).get("channels") or {}
    nuclear = nuclear or (record or {}).get("nuclear")
    out = []
    if nuclear and nuclear in channels and nuclear != marker:
        out.append({"name": nuclear, "color": NUCLEAR_MUTED_BLUE,
                    "window": channels[nuclear]["window"], "enabled": True})
    if marker in channels:
        out.append({"name": marker, "color": MARKER_COLOR,
                    "window": channels[marker]["window"], "enabled": True})
    for colour, reference in zip(REFERENCE_COLORS, references):
        if reference in channels:
            out.append({"name": reference, "color": colour,
                        "window": channels[reference]["window"], "enabled": True})
    return out


def needs_vision_check(record) -> list:
    """Channels whose calibration raised a flag worth one look."""
    return sorted(name for name, entry in ((record or {}).get("channels") or {}).items()
                  if entry.get("flags"))
