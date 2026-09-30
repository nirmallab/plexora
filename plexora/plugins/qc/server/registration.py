"""Registration Check: two nuclear channels, compared block by block.

A free QC function, run from the QC panel or by an agent through the same
capabilities (`qc.registration_*`). Two halves:

- **State** -- which nuclear channels are candidates, which is the reference,
  which the comparison, the mismatch thresholds, the overlay and flicker
  switches. It is the user's viewer state, not a QC result, so it lives in
  its own file under the QC store directory (`registration/state.json`):
  changing it never moves the QC document's revision, so a running session
  or a strictness edit is never refused over a key press.
- **Compute** -- a mismatch field: one global phase correlation between the
  two planes at an overview level, the comparison pre-aligned by its integer
  part, then ONE batched FFT phase correlation over every block. Each block's
  displacement is the global shift plus its local residual, so a cycle that
  is shifted everywhere reads as widespread, not as clean. The field is cached
  (memory, then `registration/<fp>.npz`); thresholds are applied afterwards
  (`evaluate`), so a threshold change never re-reads pixels.

`highlighted_fraction` = tissue pixels of blocks whose displacement is at or
above the threshold / tissue pixels of every evaluated block (a block is
evaluated when it is at least `min_tissue` tissue and its correlation is
confident). The denominator is named in the answer (`denominator`).

Beside the block field, the same compute keeps a MISMATCH MAP: the share of
nuclear pixels whose two stains disagree (the pixel measure the panel's stripes
use, `disagreement`), smoothed over about `MAP_SIGMA_UM` and sampled every
`MAP_CELL_UM`. It is what the panel's heatmap draws, so the heat rises wherever
disagreement crowds together -- a whole block that slid, or two cells that
moved or deformed between cycles, which no block shift can show.
`dense_mismatch_pct` = nuclear area of map cells at or above `DENSE_FRACTION`
/ nuclear area of the map.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
from collections import OrderedDict

import numpy as np

from plexora.agent.errors import AgentError

VERSION = "2"

DEFAULT_PARAMS = {"threshold_um": 2.0, "threshold_px": 4.0, "block_px": 64,
                  "min_tissue": 0.2, "min_confidence": 0.3}
#: The reference red and every comparison green -- the swatch picker's own
#: Red and Green, so the pair reads the same wherever it is shown.
DEFAULT_COLORS = {"reference": "#ff2d2d", "comparison": "#2bd46f"}
FLICKER_MS = 350
#: The level the field is measured at stays under this many pixels.
MAX_LEVEL_PIXELS = 4_500_000
#: Block states in the overlay.
NOT_EVALUATED, OK, HIGHLIGHTED, UNCERTAIN = 0, 1, 2, 3
#: A peak-to-sidelobe ratio of PSR_LOW reads as no confidence, PSR_HIGH as full.
PSR_LOW, PSR_HIGH = 4.0, 14.0

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

_GUARD = threading.Lock()
_FIELDS: "OrderedDict[str, dict]" = OrderedDict()
_FIELDS_KEEP = 8


# -- state -------------------------------------------------------------------------------


def default_state() -> dict:
    return {"active": False, "rule": {"mode": "auto", "channels": None, "pattern": None},
            "reference": None, "comparison": None, "params": dict(DEFAULT_PARAMS),
            "colors": {role: {"color": color, "user_set": False}
                       for role, color in DEFAULT_COLORS.items()},
            #: A comparison channel's own colour, by name, when the user gave it
            #: one (its row's swatch); otherwise `colors.comparison`.
            "channel_colors": {},
            "overlay_visible": True, "flicker": True, "flicker_ms": FLICKER_MS,
            "updated_at": None}


def _folder(project):
    from plexora import api

    path = api.store(project, "qc").directory() / "registration"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path, data: bytes):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def load_state(project) -> dict:
    """The stored state merged over the defaults (a missing or unreadable
    file is the default: nothing here is a result anyone would lose)."""
    state = default_state()
    path = _folder(project) / "state.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if not isinstance(stored, dict):
        return state
    for key, value in stored.items():
        if key in ("params", "rule") and isinstance(value, dict):
            state[key] = {**state[key], **value}
        elif key == "colors" and isinstance(value, dict):
            for role in DEFAULT_COLORS:
                if isinstance(value.get(role), dict):
                    state["colors"][role] = {**state["colors"][role], **value[role]}
        elif key == "channel_colors" and isinstance(value, dict):
            state[key] = {str(k): v for k, v in value.items()
                          if isinstance(v, str) and _HEX.match(v)}
        elif key in state:
            state[key] = value
    return state


def save_state(project, state) -> dict:
    from plexora.plugins.qc.server.results import now_iso

    state = {**state, "updated_at": now_iso()}
    _atomic_write(_folder(project) / "state.json",
                  json.dumps(state, sort_keys=True).encode("utf-8"))
    return state


def channel_names(record) -> list:
    return [c.get("fullname") or c.get("name") for c in record.image.real_channels]


def candidates_for(names, rule) -> tuple:
    """(candidates in channel order, method) under a detection rule."""
    from plexora.agent import presets

    rule = rule or {}
    mode = rule.get("mode") or "auto"
    if mode == "manual" and rule.get("channels"):
        wanted = set(rule["channels"])
        return [n for n in names if n in wanted], "manual_channels"
    if mode == "manual" and rule.get("pattern"):
        try:
            pattern = re.compile(rule["pattern"], re.I)
        except re.error as exc:
            raise AgentError("invalid_input", f"the DNA rule is not a valid pattern: {exc}") \
                from exc
        return [n for n in names if pattern.search(str(n))], "manual_pattern"
    return presets.nuclear_channels(names), "auto"


def _next_candidate(candidates, avoid, start=None):
    """The first candidate after `start` (wrapping) that is not `avoid`."""
    if not candidates:
        return None
    begin = candidates.index(start) + 1 if start in candidates else 0
    for offset in range(len(candidates)):
        name = candidates[(begin + offset) % len(candidates)]
        if name != avoid:
            return name
    return None


def resolve(state, names) -> dict:
    """The state with its reference / comparison made consistent with the
    image's channels and the candidates. The reference may be any channel of
    the image (the user may pick one the rule does not); the comparison
    defaults to the next candidate after the reference."""
    state = copy.deepcopy(state)
    candidates, _method = candidates_for(names, state["rule"])
    if state.get("reference") not in names:
        state["reference"] = candidates[0] if candidates else None
    if state.get("comparison") not in names or state["comparison"] == state["reference"]:
        state["comparison"] = _next_candidate(candidates, state["reference"],
                                              start=state["reference"])
    return state


def apply_update(state, names, **fields) -> dict:
    """A new state from `fields` (None = unchanged). Refuses a channel that is
    not the image's, and a reference equal to the comparison."""
    state = copy.deepcopy(state)
    was_active = bool(state.get("active"))
    for role in ("reference", "comparison"):
        value = fields.get(role)
        if value is not None and value not in names:
            raise AgentError("invalid_input", f"{value!r} is not a channel of this image",
                             detail={"channels": names[:100]})
    rule = fields.get("rule")
    if rule is not None:
        state["rule"] = {"mode": rule.get("mode") or "auto",
                         "channels": rule.get("channels"), "pattern": rule.get("pattern")}
        candidates, _ = candidates_for(names, state["rule"])
        if rule.get("channels"):
            unknown = sorted(set(rule["channels"]) - set(names))
            if unknown:
                raise AgentError("invalid_input", f"not channels of this image: {unknown}",
                                 detail={"channels": names[:100]})
        if state.get("comparison") not in candidates:
            state["comparison"] = None
        if state.get("reference") not in candidates and fields.get("reference") is None:
            state["reference"] = None
    reference, comparison = fields.get("reference"), fields.get("comparison")
    if reference is not None and comparison is not None and reference == comparison:
        raise AgentError("invalid_input", "the reference and the comparison must differ")
    if reference is not None:
        if reference == state.get("comparison") and comparison is None:
            candidates, _ = candidates_for(names, state["rule"])
            state["comparison"] = _next_candidate(candidates, reference, start=reference)
        state["reference"] = reference
    if comparison is not None:
        if comparison == state.get("reference"):
            raise AgentError("invalid_input", "the reference and the comparison must differ")
        state["comparison"] = comparison
    if fields.get("params"):
        params = {**state["params"], **{k: v for k, v in fields["params"].items()
                                        if v is not None}}
        state["params"] = params
    for key in ("overlay_visible", "flicker", "flicker_ms"):
        if fields.get(key) is not None:
            state[key] = fields[key]
    if fields.get("reset_colors"):
        state["colors"] = default_state()["colors"]
    if fields.get("reset_colors"):
        state["channel_colors"] = {}
    for role, color in (fields.get("colors") or {}).items():
        if role in DEFAULT_COLORS and color:
            state["colors"][role] = {"color": color, "user_set": True}
    own = dict(state.get("channel_colors") or {})
    for name, color in (fields.get("channel_colors") or {}).items():
        if name not in names:
            raise AgentError("invalid_input", f"{name!r} is not a channel of this image",
                             detail={"channels": names[:100]})
        if color is None:
            own.pop(name, None)
        elif _HEX.match(str(color)):
            own[name] = str(color).lower()
        else:
            raise AgentError("invalid_input", f"{color!r} is not a #rrggbb colour")
    state["channel_colors"] = own
    if fields.get("active") is not None:
        state["active"] = bool(fields["active"])
        if state["active"] and not was_active and fields.get("flicker") is None:
            # Flicker is the default on every activation.
            state["flicker"] = True
    return resolve(state, names)


def step(state, names, direction, wrap=True) -> dict:
    """Move the comparison to the previous / next candidate (never onto the
    reference)."""
    state = resolve(state, names)
    candidates, _ = candidates_for(names, state["rule"])
    pool = [n for n in candidates if n != state["reference"]]
    if not pool:
        raise AgentError("precondition_missing",
                         "there is no other nuclear channel to compare against",
                         detail={"candidates": candidates})
    current = state["comparison"]
    if current in pool:
        index = pool.index(current) + (1 if direction == "next" else -1)
        if not wrap and not 0 <= index < len(pool):
            return state
        state["comparison"] = pool[index % len(pool)]
    else:
        state["comparison"] = pool[0] if direction == "next" else pool[-1]
    return state


def status_of(state, candidates) -> str:
    if not state.get("active"):
        return "inactive"
    if not state.get("reference") or not candidates:
        return "no_candidates"
    if not state.get("comparison"):
        return "needs_second_channel"
    return "ready"


def public_state(project, record, state=None) -> dict:
    """What the panel and an agent read: the state, the candidates, the status
    and the last computed statistics for this pair (from the cache; no pixel
    is read)."""
    names = channel_names(record)
    state = resolve(state if state is not None else load_state(project), names)
    candidates, method = candidates_for(names, state["rule"])
    from plexora.server.utils import pixel_scale

    pixel = pixel_scale.pixel_size(record)
    out = {**state, "candidates": candidates, "method": method,
           "status": status_of(state, candidates), "last": None,
           "pixel_um": float(pixel["value"]) if pixel else None}
    if state["reference"] and state["comparison"]:
        cached = _cached_stats(project, record, state)
        if cached is not None:
            out["last"] = cached
    return out


def undo_arguments(project, state) -> dict:
    return {"project": project, "active": bool(state.get("active")),
            **({"reference": state["reference"]} if state.get("reference") else {}),
            **({"comparison": state["comparison"]} if state.get("comparison") else {}),
            "rule": state.get("rule"), "params": state.get("params"),
            "overlay_visible": state.get("overlay_visible"), "flicker": state.get("flicker"),
            "flicker_ms": state.get("flicker_ms"),
            "channel_colors": dict(state.get("channel_colors") or {})}


# -- the mismatch field --------------------------------------------------------------------


def _hann2(h, w):
    return np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)


def _subpixel(c_minus, c0, c_plus):
    denom = c_minus - 2.0 * c0 + c_plus
    with np.errstate(divide="ignore", invalid="ignore"):
        offset = np.where(np.abs(denom) > 1e-12, 0.5 * (c_minus - c_plus) / denom, 0.0)
    return np.clip(np.nan_to_num(offset), -0.5, 0.5)


def phase_correlate(reference, moving):
    """Batched phase correlation. `reference`/`moving` are (N, h, w) or (h, w)
    float arrays; returns (dy, dx, psr), each (N,) or scalar: the displacement
    of `moving` against `reference` (moving(x) ~ reference(x - d)), with a
    3-point parabolic sub-pixel fit and the correlation's peak-to-sidelobe
    ratio."""
    from scipy import fft

    single = reference.ndim == 2
    a = np.asarray(reference, dtype=np.float32)
    b = np.asarray(moving, dtype=np.float32)
    if single:
        a, b = a[None], b[None]
    n, h, w = a.shape
    window = _hann2(h, w)
    a = (a - a.mean(axis=(1, 2), keepdims=True)) * window
    b = (b - b.mean(axis=(1, 2), keepdims=True)) * window
    fa = fft.rfft2(a, axes=(1, 2), workers=-1)
    fb = fft.rfft2(b, axes=(1, 2), workers=-1)
    cross = fb * np.conj(fa)
    magnitude = np.abs(cross)
    cross /= np.maximum(magnitude, 1e-12)
    surface = fft.irfft2(cross, s=(h, w), axes=(1, 2), workers=-1).astype(np.float32)
    flat = surface.reshape(n, -1)
    peak = np.argmax(flat, axis=1)
    py, px = np.divmod(peak, w)
    rows = np.arange(n)
    c0 = flat[rows, peak]
    fy = _subpixel(surface[rows, (py - 1) % h, px], c0, surface[rows, (py + 1) % h, px])
    fx = _subpixel(surface[rows, py, (px - 1) % w], c0, surface[rows, py, (px + 1) % w])
    dy = np.where(py > h // 2, py - h, py) + fy
    dx = np.where(px > w // 2, px - w, px) + fx
    # Peak-to-sidelobe: the peak against the surface outside a 5x5 window.
    ys = (py[:, None] + np.arange(-2, 3)[None]) % h
    xs = (px[:, None] + np.arange(-2, 3)[None]) % w
    keep = np.ones_like(surface, dtype=bool)
    keep[rows[:, None, None], ys[:, :, None], xs[:, None, :]] = False
    count = keep.reshape(n, -1).sum(axis=1)
    side = np.where(keep, surface, 0.0).reshape(n, -1)
    mean = side.sum(axis=1) / count
    var = np.maximum((np.where(keep, surface, 0.0) ** 2).reshape(n, -1).sum(axis=1) / count
                     - mean ** 2, 1e-12)
    psr = (c0 - mean) / np.sqrt(var)
    if single:
        return float(dy[0]), float(dx[0]), float(psr[0])
    return dy.astype(np.float32), dx.astype(np.float32), psr.astype(np.float32)


def _confidence(psr):
    return np.clip((np.asarray(psr, dtype=np.float32) - PSR_LOW) / (PSR_HIGH - PSR_LOW), 0, 1)


def _shift_integer(plane, dy, dx):
    """`plane` moved by (-dy, -dx) whole pixels, zero-filled (a slice copy)."""
    out = np.zeros_like(plane)
    h, w = plane.shape
    sy, sx = int(dy), int(dx)
    src_y = slice(max(0, sy), min(h, h + sy))
    dst_y = slice(max(0, -sy), min(h, h - sy))
    src_x = slice(max(0, sx), min(w, w + sx))
    dst_x = slice(max(0, -sx), min(w, w - sx))
    out[dst_y, dst_x] = plane[src_y, src_x]
    return out


def mismatch_field(reference, comparison, tissue, block_px) -> dict:
    """The displacement field of `comparison` against `reference` (both
    already smoothed, same shape), at the planes' own pixel size.

    Returns {global: {dy, dx, confidence}, grid: {x0, y0, step, nx, ny},
    dy, dx, confidence, tissue} -- per-block arrays (ny, nx)."""
    h, w = reference.shape
    gdy, gdx, gpsr = phase_correlate(reference, comparison)
    iy, ix = int(round(gdy)), int(round(gdx))
    aligned = _shift_integer(comparison, iy, ix)
    b = int(min(max(block_px, 24), 256, h, w))
    ny, nx = h // b, w // b
    if ny < 1 or nx < 1:
        raise AgentError("invalid_input", "the image is too small for a registration check")
    y0, x0 = (h - ny * b) // 2, (w - nx * b) // 2

    def blocks(plane):
        cut = plane[y0:y0 + ny * b, x0:x0 + nx * b]
        return cut.reshape(ny, b, nx, b).transpose(0, 2, 1, 3).reshape(ny * nx, b, b)

    dy, dx, psr = phase_correlate(blocks(reference), blocks(aligned))
    tissue_fraction = blocks(tissue.astype(np.float32)).mean(axis=(1, 2))
    # A block the zero fill reaches has nothing to compare there.
    valid = np.ones((h, w), dtype=np.float32)
    valid = _shift_integer(valid, iy, ix)
    covered = blocks(valid).mean(axis=(1, 2))
    confidence = _confidence(psr) * (covered >= 0.9)
    return {"global": {"dy": float(gdy), "dx": float(gdx),
                       "confidence": float(_confidence(gpsr))},
            "grid": {"x0": int(x0), "y0": int(y0), "step": int(b), "nx": int(nx),
                     "ny": int(ny)},
            "dy": (dy + iy).reshape(ny, nx).astype(np.float32),
            "dx": (dx + ix).reshape(ny, nx).astype(np.float32),
            "confidence": confidence.reshape(ny, nx).astype(np.float32),
            "tissue": tissue_fraction.reshape(ny, nx).astype(np.float32)}


def evaluate(field, factor, pixel_um, params) -> tuple:
    """(stats, state grid) for a field under thresholds; cheap."""
    from scipy import ndimage

    params = {**DEFAULT_PARAMS, **(params or {})}
    dy = field["dy"] * factor
    dx = field["dx"] * factor
    magnitude = np.hypot(dy, dx)
    if pixel_um:
        threshold_px = float(params["threshold_um"]) / float(pixel_um)
        unit = "um"
    else:
        threshold_px = float(params["threshold_px"])
        unit = "px"
    # Sub-pixel accuracy at the measured level is about a third of a pixel.
    sensitivity_px = 0.3 * factor
    effective = max(threshold_px, sensitivity_px)
    tissue = field["tissue"]
    state = np.full(tissue.shape, NOT_EVALUATED, dtype=np.uint8)
    on_tissue = tissue >= float(params["min_tissue"])
    confident = field["confidence"] >= float(params["min_confidence"])
    state[on_tissue & ~confident] = UNCERTAIN
    state[on_tissue & confident] = OK
    state[on_tissue & confident & (magnitude >= effective)] = HIGHLIGHTED
    weight = tissue * (field["grid"]["step"] ** 2)
    evaluated = float(weight[(state == OK) | (state == HIGHLIGHTED)].sum())
    highlighted = float(weight[state == HIGHLIGHTED].sum())
    all_tissue = float(weight[on_tissue].sum())
    fraction = highlighted / evaluated if evaluated else None
    lit = state == HIGHLIGHTED
    components = int(ndimage.label(lit)[1]) if lit.any() else 0
    if not lit.any():
        pattern = "none"
    elif fraction is not None and fraction >= 0.5:
        pattern = "widespread"
    else:
        pattern = "isolated"
    measured = magnitude[(state == OK) | (state == HIGHLIGHTED)]

    def um(px):
        return round(float(px) * float(pixel_um), 3) if pixel_um else None

    g = field["global"]
    global_px = float(np.hypot(g["dy"], g["dx"]) * factor)
    stats = {
        "highlighted_fraction": None if fraction is None else round(fraction, 4),
        "highlighted_pct": None if fraction is None else round(100.0 * fraction, 1),
        "denominator": "evaluated_tissue",
        "uncertain_fraction": round(float(weight[state == UNCERTAIN].sum()) / all_tissue, 4)
        if all_tissue else None,
        "evaluated_fraction_of_tissue": round(evaluated / all_tissue, 4) if all_tissue
        else None,
        "blocks": {"total": int(state.size), "evaluated": int(((state == OK) | lit).sum()),
                   "highlighted": int(lit.sum()), "uncertain": int((state == UNCERTAIN).sum()),
                   "not_evaluated": int((state == NOT_EVALUATED).sum())},
        "components": components, "pattern": pattern,
        "threshold_px": round(threshold_px, 3), "sensitivity_px": round(sensitivity_px, 3),
        "effective_threshold_px": round(effective, 3), "unit": unit,
        "threshold_um": float(params["threshold_um"]) if pixel_um else None,
        "global_shift_px": {"dy": round(g["dy"] * factor, 2), "dx": round(g["dx"] * factor, 2),
                            "magnitude": round(global_px, 2),
                            "confidence": round(g["confidence"], 3)},
        "global_shift_um": um(global_px),
        "dense_mismatch_pct": _dense_pct(field.get("map")),
        "mismatch_hotspots": _hotspots(field.get("map"), factor, pixel_um),
        "residual_px": ({"p50": round(float(np.percentile(measured, 50)), 2),
                         "p90": round(float(np.percentile(measured, 90)), 2),
                         "max": round(float(measured.max()), 2)} if measured.size else None),
    }
    return stats, state


def _dense_pct(mapped):
    """% of the map's nuclear area in cells at or above `DENSE_FRACTION`."""
    if not mapped:
        return None
    nucleus = mapped["nucleus"]
    total = float(nucleus[nucleus >= MAP_MIN_NUCLEUS].sum())
    if not total:
        return None
    dense = (mapped["share"] >= DENSE_FRACTION) & (nucleus >= MAP_MIN_NUCLEUS)
    return round(100.0 * float(nucleus[dense].sum()) / total, 2)


#: At most this many hotspots are named, each this far from the others.
HOTSPOTS = 8
HOTSPOT_SPACING_UM = 60.0
HOTSPOT_SPACING_PX = 90.0


def _hotspots(mapped, factor, pixel_um) -> list:
    """The map's densest places, full-resolution pixels, worst first: where an
    agent should look (`render_region`) before believing `highlighted_pct`."""
    if not mapped:
        return []
    share = np.where(mapped["nucleus"] >= MAP_MIN_NUCLEUS, mapped["share"], 0.0)
    grid = mapped["grid"]
    spacing = (HOTSPOT_SPACING_UM / pixel_um if pixel_um else HOTSPOT_SPACING_PX)
    order = np.argsort(-share, axis=None)
    found = []
    for flat in order[:4000]:
        value = float(share.flat[flat])
        if value < DENSE_FRACTION or len(found) >= HOTSPOTS:
            break
        y, x = divmod(int(flat), grid["nx"])
        cx = (grid["x0"] + (x + 0.5) * grid["step"]) * factor
        cy = (grid["y0"] + (y + 0.5) * grid["step"]) * factor
        if any(abs(cx - f["x"]) < spacing and abs(cy - f["y"]) < spacing for f in found):
            continue
        found.append({"x": round(cx, 1), "y": round(cy, 1), "share": round(value, 3)})
    return found


def map_overlay(mapped, factor) -> dict | None:
    """The mismatch map for the panel: its grid in full-resolution pixels and
    two byte planes, base64 (row-major, ny x nx): `share` (0..255 = 0..100%
    of nuclear pixels disagreeing) and `nucleus` (0..255 = the smoothed
    nuclear share, which fades the heat at the tissue's edges)."""
    import base64

    if not mapped:
        return None
    grid = mapped["grid"]

    def packed(plane):
        data = np.round(np.clip(plane, 0.0, 1.0) * 255).astype(np.uint8)
        return base64.b64encode(data.tobytes()).decode("ascii")

    return {"grid": {"x0": round(grid["x0"] * factor, 2), "y0": round(grid["y0"] * factor, 2),
                     "step_px": round(grid["step"] * factor, 3), "nx": grid["nx"],
                     "ny": grid["ny"]},
            "share": packed(mapped["share"]), "nucleus": packed(mapped["nucleus"]),
            "min_nucleus": MAP_MIN_NUCLEUS, "dense_fraction": DENSE_FRACTION}


def overlay_of(field, factor, state) -> dict:
    """The blocks in full-resolution pixels, for the panel to draw, and the
    mismatch map (`map_overlay`) its heatmap draws."""
    grid = field["grid"]
    return {"grid": {"x0": round(grid["x0"] * factor, 2), "y0": round(grid["y0"] * factor, 2),
                     "step_px": round(grid["step"] * factor, 2), "nx": grid["nx"],
                     "ny": grid["ny"]},
            "dx_px": np.round(field["dx"] * factor, 2).ravel().tolist(),
            "dy_px": np.round(field["dy"] * factor, 2).ravel().tolist(),
            "confidence": np.round(field["confidence"], 3).ravel().tolist(),
            "tissue": np.round(field["tissue"], 3).ravel().tolist(),
            "state": state.ravel().astype(int).tolist(),
            "mismatch": map_overlay(field.get("map"), factor)}


# -- planning, caching and compute ---------------------------------------------------------


def _stamp(identity):
    if identity and not str(identity).startswith("node://"):
        try:
            stat = os.stat(identity)
            return f"{stat.st_size}-{stat.st_mtime_ns}"
        except OSError:
            return None
    return None


def _plan(session, project, record, state):
    from plexora.agent.render import resolve_channel
    from plexora.server.utils import pixel_scale, source_image

    if record.image.is_blank:
        raise AgentError("unsupported_modality", "this sample has no image to check")
    names = channel_names(record)
    state = resolve(state, names)
    reference, comparison = state.get("reference"), state.get("comparison")
    if not reference:
        raise AgentError("precondition_missing",
                         "no nuclear channel was found: set a reference channel or a DNA "
                         "rule (set_registration_check)", detail={"channels": names[:100]})
    if not comparison:
        raise AgentError("precondition_missing",
                         "only one nuclear channel: pick a comparison channel "
                         "(set_registration_check comparison=...)",
                         detail={"reference": reference, "channels": names[:100]})
    channels = list(record.image.real_channels)
    keys = {role: source_image.channel_key(resolve_channel(name, channels)[1])
            for role, name in (("reference", reference), ("comparison", comparison))}
    image_data = session.image_data(project)
    identity = source_image.ReaderShelf.identity_of(image_data)
    pixel = pixel_scale.pixel_size(record)
    pixel_um = float(pixel["value"]) if pixel else None
    return state, keys, image_data, identity, pixel_um


def _choose_level(source, threshold_full_px):
    from plexora.agent.evidence import image_qc

    level = image_qc.overview_level(source, max_pixels=MAX_LEVEL_PIXELS)
    width0 = source.level_shape(0)[1]
    while level > 0:
        factor = width0 / source.level_shape(level)[1]
        h, w = source.level_shape(level - 1)
        if threshold_full_px / factor >= 1.0 or h * w > MAX_LEVEL_PIXELS:
            break
        level -= 1
    return level, width0 / source.level_shape(level)[1]


#: The mismatch map: the disagreement share smoothed over this (microns; the
#: `_PX` figure in full-resolution pixels without a pixel size) ...
MAP_SIGMA_UM = 8.0
MAP_SIGMA_PX = 12.0
#: ... and sampled every this many.
MAP_CELL_UM = 6.5
MAP_CELL_PX = 10.0
#: A cell whose smoothed nuclear share is under this reads as no tissue.
MAP_MIN_NUCLEUS = 0.05


def normalised(plane, window):
    """A stain as the disagreement reads it: log1p, windowed to 0..1."""
    lo, hi = window
    logged = np.log1p(np.maximum(np.asarray(plane, dtype=np.float32), 0))
    return np.clip((logged - lo) / (hi - lo), 0.0, 1.0)


def lit_of(a, b):
    """The pixel disagreement of two normalised stains (0..1), unsmoothed in,
    lightly smoothed out: the stripes' measure."""
    from scipy import ndimage

    a, b = (ndimage.gaussian_filter(p, 0.8) for p in (a, b))
    return a, b, np.clip((np.abs(a - b) - DISAGREE_LOW) / (DISAGREE_HIGH - DISAGREE_LOW),
                         0.0, 1.0)


def mismatch_map(a, b, sigma_px, cell_px) -> dict:
    """The mismatch map of two normalised stains (same shape), in their own
    pixels: {grid: {x0, y0, step, nx, ny}, share, nucleus} -- per cell, the
    share of nuclear pixels that disagree and the nuclear share itself, both
    smoothed over `sigma_px`."""
    from scipy import ndimage

    a, b, lit = lit_of(a, b)
    nucleus = (np.maximum(a, b) >= DENSE_NUCLEUS).astype(np.float32)
    wrong = ((lit >= DENSE_LIT) & (nucleus > 0)).astype(np.float32)
    sigma = max(1.0, float(sigma_px))
    nuc_s = ndimage.gaussian_filter(nucleus, sigma)
    wrong_s = ndimage.gaussian_filter(wrong, sigma)
    share = np.where(nuc_s >= MAP_MIN_NUCLEUS, wrong_s / np.maximum(nuc_s, 1e-6), 0.0)
    step = max(1, int(round(cell_px)))
    h, w = share.shape
    ny, nx = max(1, h // step), max(1, w // step)
    y0, x0 = (h - ny * step) // 2, (w - nx * step) // 2

    def cells(plane):
        cut = plane[y0:y0 + ny * step, x0:x0 + nx * step]
        return cut.reshape(ny, step, nx, step).mean(axis=(1, 3)).astype(np.float32)

    return {"grid": {"x0": int(x0), "y0": int(y0), "step": int(step), "nx": int(nx),
                     "ny": int(ny)},
            "share": np.clip(cells(share), 0.0, 1.0), "nucleus": cells(nuc_s)}


def _field_fp(identity, stamp, keys, level, shape, block_px):
    blob = json.dumps({"v": VERSION, "identity": identity, "stamp": stamp,
                       "keys": keys, "level": level, "shape": list(shape),
                       "block_px": int(block_px)}, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _remember(fp, entry):
    with _GUARD:
        _FIELDS[fp] = entry
        _FIELDS.move_to_end(fp)
        while len(_FIELDS) > _FIELDS_KEEP:
            _FIELDS.popitem(last=False)


def _load_field(project, fp):
    with _GUARD:
        held = _FIELDS.get(fp)
    if held is not None:
        return held
    folder = _folder(project)
    try:
        meta = json.loads((folder / f"{fp}.json").read_text(encoding="utf-8"))
        with np.load(folder / f"{fp}.npz", allow_pickle=False) as arrays:
            field = {k: arrays[k] for k in ("dy", "dx", "confidence", "tissue")}
            field["map"] = {"grid": meta["map_grid"], "share": arrays["map_share"],
                            "nucleus": arrays["map_nucleus"]}
    except (OSError, ValueError, KeyError):
        return None
    if meta.get("version") != VERSION:
        return None
    field["global"] = meta["global"]
    field["grid"] = meta["grid"]
    entry = {"field": field, "factor": meta["factor"], "level": meta["level"],
             "computed_at": meta["computed_at"], "timing_ms": meta.get("timing_ms")}
    _remember(fp, entry)
    return entry


def _save_field(project, fp, entry):
    import io

    folder = _folder(project)
    field = entry["field"]
    buffer = io.BytesIO()
    np.savez_compressed(buffer, **{k: field[k] for k in ("dy", "dx", "confidence", "tissue")},
                        map_share=field["map"]["share"], map_nucleus=field["map"]["nucleus"])
    _atomic_write(folder / f"{fp}.npz", buffer.getvalue())
    meta = {"version": VERSION, "global": field["global"], "grid": field["grid"],
            "map_grid": field["map"]["grid"],
            "factor": entry["factor"], "level": entry["level"],
            "computed_at": entry["computed_at"], "timing_ms": entry.get("timing_ms")}
    _atomic_write(folder / f"{fp}.json", json.dumps(meta).encode("utf-8"))
    _sweep(folder)


def _sweep(folder, keep=4):
    metas = sorted((p for p in folder.glob("*.json") if p.name not in ("state.json",
                                                                         "last.json")),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in metas[keep:]:
        for path in (stale, stale.with_suffix(".npz")):
            try:
                path.unlink()
            except OSError:
                pass


def _last_key(keys, block_px):
    return f"{keys['reference']}|{keys['comparison']}|{int(block_px)}"


def _note_last(project, keys, block_px, fp):
    """Which field a pair was last measured as, so a status read finds the
    cached field without opening the image (`registration/last.json`)."""
    path = _folder(project) / "last.json"
    with _GUARD:
        try:
            known = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            known = {}
        known[_last_key(keys, block_px)] = fp
        _atomic_write(path, json.dumps(dict(list(known.items())[-32:])).encode("utf-8"))


def _last_fp(project, keys, block_px):
    try:
        known = json.loads((_folder(project) / "last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return known.get(_last_key(keys, block_px))


def _stats_fp(field_fp, params, pixel_um):
    keys = ("threshold_um", "threshold_px", "min_tissue", "min_confidence")
    blob = json.dumps({"field": field_fp, "pixel_um": pixel_um,
                       **{k: params.get(k) for k in keys}}, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _cached_stats(project, record, state):
    try:
        from plexora.agent.render import resolve_channel
        from plexora.server.utils import pixel_scale, source_image

        channels = list(record.image.real_channels)
        keys = {role: source_image.channel_key(resolve_channel(state[role], channels)[1])
                for role in ("reference", "comparison")}
    except Exception:
        return None
    fp = _last_fp(project, keys, state["params"]["block_px"])
    if fp is None:
        return None
    entry = _load_field(project, fp)
    if entry is None:
        return None
    pixel = pixel_scale.pixel_size(record)
    pixel_um = float(pixel["value"]) if pixel else None
    stats, _state = evaluate(entry["field"], entry["factor"], pixel_um, state["params"])
    return {"fingerprint": _stats_fp(fp, state["params"], pixel_um),
            "computed_at": entry["computed_at"], "reference": state["reference"],
            "comparison": state["comparison"], "stats": stats}


def compute(session, project, state, *, force=False, include_overlay=True,
            comparison=None) -> dict:
    """The mismatch field of the state's pair, reused when the image, the pair,
    the level and the block size have not changed. `comparison` measures that
    channel against the reference instead, without changing the state (the
    panel's per-channel scores)."""
    from time import perf_counter

    from plexora.plugins.qc.server import scan
    from plexora.plugins.qc.server.results import now_iso
    from plexora.server.utils import source_image
    from scipy import ndimage

    record = session.project(project)
    if comparison is not None:
        state = _with_comparison(state, record, comparison)
    state, keys, image_data, identity, pixel_um = _plan(session, project, record, state)
    params = {**DEFAULT_PARAMS, **state["params"]}
    threshold_full = float(params["threshold_um"]) / pixel_um if pixel_um \
        else float(params["threshold_px"])
    stamp = _stamp(identity)
    sigma_full = MAP_SIGMA_UM / pixel_um if pixel_um else MAP_SIGMA_PX
    cell_full = MAP_CELL_UM / pixel_um if pixel_um else MAP_CELL_PX
    started = perf_counter()
    with source_image.SHELF.reader(image_data) as source:
        if source.is_brightfield:
            raise AgentError("unsupported_modality",
                             "a brightfield image has no nuclear channels to register")
        level, factor = _choose_level(source, threshold_full)
        shape = source.level_shape(level)
        fp = _field_fp(identity, stamp, keys, level, shape, params["block_px"])
        entry = None if force else _load_field(project, fp)
        reused = entry is not None
        if entry is None:
            from plexora.agent.evidence import image_qc

            raw, planes, stains = {}, {}, {}
            overview = image_qc.overview_level(source, max_pixels=MAX_LEVEL_PIXELS)
            for role in ("reference", "comparison"):
                raw[role], _level, _ceiling = image_qc.read_overview(source, keys[role], level)
                planes[role] = ndimage.gaussian_filter(np.log1p(np.maximum(raw[role], 0)), 1.0)
                window = _window(source, keys[role], overview,
                                 json.dumps([identity, stamp, keys[role], overview]))
                stains[role] = normalised(raw[role], window)
    if entry is None:
        tissue = scan.tissue_estimate({state["reference"]: raw["reference"]},
                                      state["reference"])["mask"]
        del raw
        field = mismatch_field(planes["reference"], planes["comparison"], tissue,
                               int(params["block_px"]))
        field["map"] = mismatch_map(stains["reference"], stains["comparison"],
                                    sigma_full / factor, cell_full / factor)
        del stains
        entry = {"field": field, "factor": float(factor), "level": int(level),
                 "computed_at": now_iso(),
                 "timing_ms": round((perf_counter() - started) * 1000.0, 1)}
        _remember(fp, entry)
        _save_field(project, fp, entry)
    _note_last(project, keys, params["block_px"], fp)
    stats, grid_state = evaluate(entry["field"], entry["factor"], pixel_um, params)
    stats["level"] = entry["level"]
    stats["level_factor"] = round(entry["factor"], 3)
    stats["timing_ms"] = entry.get("timing_ms")
    out = {"project": project, "reference": state["reference"],
           "comparison": state["comparison"], "stats": stats, "reused": reused,
           "fingerprint": _stats_fp(fp, params, pixel_um), "field_fingerprint": fp,
           "computed_at": entry["computed_at"]}
    if include_overlay:
        out["overlay"] = overlay_of(entry["field"], entry["factor"], grid_state)
    return out


def _with_comparison(state, record, comparison):
    """The state measuring `comparison` against its reference (not stored)."""
    names = channel_names(record)
    if comparison not in names:
        raise AgentError("invalid_input", f"{comparison!r} is not a channel of this image",
                         detail={"channels": names[:100]})
    state = resolve(state, names)
    if comparison == state.get("reference"):
        raise AgentError("invalid_input", "the reference and the comparison must differ")
    return {**state, "comparison": comparison}


# -- the disagreement raster ----------------------------------------------------------------

#: The longest side a disagreement raster is drawn at.
MAX_RASTER_PX = 2048
#: A pixel is lit where the two normalised stains differ by at least this ...
DISAGREE_LOW = 0.18
#: ... and fully lit from this.
DISAGREE_HIGH = 0.6
#: Amber, the highlight colour the panel has always drawn mismatch in.
HIGHLIGHT_RGB = (245, 158, 11)
#: A patch this wide (microns; `DENSE_WINDOW_PX` full-resolution pixels when the
#: image has no pixel size) is DENSE when at least `DENSE_FRACTION` of its
#: nuclear pixels disagree and at least `DENSE_MIN_TISSUE` of it is nucleus.
#: That is a cell or two that moved, deformed or lifted between the cycles:
#: too small to move an 80 um block's shift, but plainly wrong on screen.
DENSE_WINDOW_UM = 25.0
DENSE_WINDOW_PX = 40
DENSE_FRACTION = 0.15
DENSE_MIN_TISSUE = 0.1
#: A pixel is nucleus where either stain reaches this, and counts as
#: disagreeing where `lit` reaches `DENSE_LIT`.
DENSE_NUCLEUS = 0.25
DENSE_LIT = 0.3
_WINDOWS: "OrderedDict[str, tuple]" = OrderedDict()


def _window(source, key, level, cache_key):
    """(lo, hi) of log1p intensity for one channel, from its overview: the
    median (mostly background) to the 99.7th percentile (bright nuclei). Held
    per image and channel, so every view of it is normalised the same way and
    a pan never changes what counts as disagreement."""
    from plexora.agent.evidence import image_qc

    with _GUARD:
        held = _WINDOWS.get(cache_key)
    if held is not None:
        return held
    plane, _level, _ceiling = image_qc.read_overview(source, key, level)
    logged = np.log1p(np.maximum(plane, 0))
    lo, hi = (float(v) for v in np.percentile(logged, [50.0, 99.7]))
    window = (lo, max(hi, lo + 1e-6))
    with _GUARD:
        _WINDOWS[cache_key] = window
        while len(_WINDOWS) > 32:
            _WINDOWS.popitem(last=False)
    return window


def _level_count(source):
    count = 1
    while True:
        try:
            shape = source.level_shape(count)
        except Exception:
            return count
        if shape == source.level_shape(count - 1) or count > 30:
            return count
        count += 1


def raster_level(source, box_width, max_px):
    """(level, factor): the finest level at which `box_width` full-resolution
    pixels fit in `max_px` -- the pyramid level the viewer itself draws a
    view that wide at."""
    width0 = source.level_shape(0)[1]
    last = _level_count(source) - 1
    level = 0
    while level < last and box_width * source.level_shape(level)[1] / width0 > max_px:
        level += 1
    return level, width0 / source.level_shape(level)[1]


def dense_mismatch(a, b, lit, window_px) -> np.ndarray:
    """Where the disagreement is dense: each pixel's `window_px` neighbourhood
    has at least `DENSE_FRACTION` of its nuclear pixels disagreeing (and is at
    least `DENSE_MIN_TISSUE` nucleus). `a` and `b` are the two normalised
    stains, `lit` the disagreement; all the same shape."""
    from scipy import ndimage

    size = max(3, int(round(window_px)))
    nucleus = (np.maximum(a, b) >= DENSE_NUCLEUS).astype(np.float32)
    wrong = ((lit >= DENSE_LIT) & (nucleus > 0)).astype(np.float32)
    share = ndimage.uniform_filter(nucleus, size, mode="constant")
    wrong_share = ndimage.uniform_filter(wrong, size, mode="constant")
    return (share >= DENSE_MIN_TISSUE) & (wrong_share >= DENSE_FRACTION * np.maximum(share, 1e-6))


def disagreement(session, project, state, box, *, max_px=1024, comparison=None) -> dict:
    """Where the reference and comparison DNA stains disagree, pixel by pixel,
    over `box` (x0, y0, x1, y1 in full-resolution pixels), read at the pyramid
    level that puts the box in about `max_px` pixels.

    Each stain is log-scaled and windowed on its own overview (`_window`), so a
    dimmer cycle does not read as a moved one; what is lit is where one stain
    has nucleus and the other has none -- the crescents a shift leaves on
    every nucleus it moves. Returns {png, box, level, factor, ...}: an RGBA
    PNG in the highlight colour whose alpha is the disagreement, and the box it
    covers (clipped to the image, in full-resolution pixels). The blue channel
    is 255 where the pixel lies in a dense patch (`dense_mismatch`), which the
    panel stripes wherever it is; the rest only inside a displaced block."""
    import io

    from PIL import Image
    from scipy import ndimage

    from plexora.agent.evidence import image_qc
    from plexora.server.utils import source_image

    record = session.project(project)
    if comparison is not None:
        state = _with_comparison(state, record, comparison)
    state, keys, image_data, identity, pixel_um = _plan(session, project, record, state)
    max_px = int(min(max(64, int(max_px)), MAX_RASTER_PX))
    stamp = _stamp(identity)
    with source_image.SHELF.reader(image_data) as source:
        if source.is_brightfield:
            raise AgentError("unsupported_modality",
                             "a brightfield image has no nuclear channels to register")
        height0, width0 = source.level_shape(0)
        x0 = max(0.0, min(float(box[0]), width0))
        y0 = max(0.0, min(float(box[1]), height0))
        x1 = max(x0, min(float(box[2]), width0))
        y1 = max(y0, min(float(box[3]), height0))
        if x1 - x0 < 1 or y1 - y0 < 1:
            raise AgentError("invalid_input", "the box is outside the image")
        level, factor = raster_level(source, max(x1 - x0, y1 - y0), max_px)
        overview = image_qc.overview_level(source, max_pixels=MAX_LEVEL_PIXELS)
        lbox = (x0 / factor, y0 / factor, x1 / factor, y1 / factor)
        planes = []
        clipped = None
        for role in ("reference", "comparison"):
            index = source.channel_index(keys[role])
            if index is None:
                raise AgentError("invalid_input",
                                 f"{state[role]!r} is not a channel of this image")
            lo, hi = _window(source, keys[role], overview,
                             json.dumps([identity, stamp, keys[role], overview]))
            plane, clipped = source.read(index, level, lbox)
            planes.append(normalised(plane, (lo, hi)))
    h = min(p.shape[0] for p in planes)
    w = min(p.shape[1] for p in planes)
    a, b, lit = lit_of(planes[0][:h, :w], planes[1][:h, :w])
    window_full = DENSE_WINDOW_UM / pixel_um if pixel_um else DENSE_WINDOW_PX
    dense = dense_mismatch(a, b, lit, window_full / factor) & (lit > 0)
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[..., 0], rgba[..., 1], rgba[..., 2] = HIGHLIGHT_RGB
    rgba[..., 2][dense] = 255
    rgba[..., 3] = np.round(lit * 255).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buffer, format="PNG", compress_level=3)
    cx0, cy0 = clipped[0] * factor, clipped[1] * factor
    return {"png": buffer.getvalue(), "level": int(level), "factor": float(factor),
            "box": [round(cx0, 2), round(cy0, 2), round(cx0 + w * factor, 2),
                    round(cy0 + h * factor, 2)],
            "reference": state["reference"], "comparison": state["comparison"],
            "lit_fraction": round(float((lit > 0).mean()), 4) if lit.size else 0.0,
            "dense_fraction": round(float(dense.mean()), 4) if dense.size else 0.0}
