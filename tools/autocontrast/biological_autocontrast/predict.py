"""predict_contrast: per-channel display windows for one image plane.

With a checkpoint the window is the model's; without one it is Plexora's
percentile calibration, labelled `heuristic_percentile` with
`model_available: false` everywhere it is written, so a heuristic proposal is
never mistaken for a learned one.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from biological_autocontrast import baselines, features as feat
from biological_autocontrast.config import GAMMA, Config, home, versions
from biological_autocontrast.domain import to_norm, to_raw
from biological_autocontrast.io import fingerprint_of, open_image, plane_or_warn

#: A model's white point may sit a little above full scale (it never clips).
MAX_MARGIN = 0.0
MIN_SPAN = 1e-6


def _cache_key(path, channel_indices, z, t, seed, checkpoint_id, cfg, assume_bits) -> str:
    blob = json.dumps({
        "fingerprint": fingerprint_of(path),
        "path": str(path) if str(path).startswith("node://") else str(Path(path).resolve()),
        "channels": None if channel_indices is None else [int(c) for c in channel_indices],
        "z": z, "t": t, "seed": int(seed), "checkpoint": checkpoint_id,
        "config": cfg.digest(), "assume_bits": assume_bits, **versions()}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _cache_path(key) -> Path:
    return home() / "cache" / "predict" / f"{key}.json"


def _checkpoint_id(path):
    if not path:
        return "heuristic"
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


def _bounded(min_norm, max_norm, upper):
    """A window that is finite, ordered and inside [0, upper]."""
    min_norm = float(min_norm) if np.isfinite(min_norm) else 0.0
    max_norm = float(max_norm) if np.isfinite(max_norm) else upper
    min_norm = min(max(min_norm, 0.0), upper - MIN_SPAN)
    max_norm = min(max(max_norm, min_norm + MIN_SPAN), upper)
    return min_norm, max_norm


def _heuristic(windows, preferred):
    for name in (preferred, "percentile", "fixed", "gmm"):
        if windows.get(name):
            return name, windows[name]
    return None, None


def predict_contrast(image_path, checkpoint_path=None, channel_indices=None, z=None, t=None,
                     seed=42, config=None, force=False, assume_bits=None):
    """One dict per channel (see docs/internal/AUTOCONTRAST.md, "Proposal row")."""
    rows, _features, _ctx = run(image_path, checkpoint_path, channel_indices, z=z, t=t,
                                seed=seed, config=config, force=force, assume_bits=assume_bits)
    return rows


def run(image_path, checkpoint_path=None, channel_indices=None, *, z=None, t=None, seed=42,
        config=None, force=False, assume_bits=None, keep_features=False):
    """(rows, {channel: ChannelFeatures} when computed, ImageContext or None).

    A cache hit opens nothing: the key is the file's size and mtime plus every
    input and version, all of which are known without a read."""
    cfg = config or Config()
    checkpoint_id = _checkpoint_id(checkpoint_path)
    key = _cache_key(image_path, channel_indices, z, t, seed, checkpoint_id, cfg, assume_bits)
    cached = _cache_path(key)
    if cached.exists() and not force and not keep_features:
        return json.loads(cached.read_text(encoding="utf-8")), {}, None

    model = None
    if checkpoint_path:
        from biological_autocontrast.model import ContrastModel

        model = ContrastModel(checkpoint_path)

    from biological_autocontrast.model import data_abstain

    computed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows, kept = [], {}
    with open_image(image_path) as image:
        zz, tt, plane_warnings = plane_or_warn(image, z, t)
        indices = (list(range(image.n_channels)) if channel_indices is None
                   else [image.channel_index(c) for c in channel_indices])
        ctx = feat.ImageContext.build(image, zz, tt, cfg, plane_warnings)
        image_id = Path(image_path).name
        for c in indices:
            f = feat.extract_channel(ctx, c, seed=seed, assume_bits=assume_bits)
            nuclear = image.nuclear_channel() == c
            windows = baselines.all_windows(f.overview_raw, nuclear)
            reason, flags = data_abstain(f, ctx, cfg)
            dom = f.domain
            upper = 1.0 if dom.normalization_method == "fixed_scale" else 2.0
            confidence = None
            if model is not None:
                batch = feat.pack([{"patches": f.patches, "categories": feat.categories_of(f.selection),
                                    "hist": f.hist, "quant": f.quant, "dom": f.dom,
                                    "dom_mask": f.dom_mask}], model.n_patches, model.patch_size)
                out = model.predict(batch)
                min_norm, max_norm = out["min_norm"][0], out["max_norm"][0]
                confidence = float(out["confidence"][0])
                method = "model"
                if reason is None and out["abstain"][0] >= 0.5:
                    reason = "model_abstain"
                if reason is None and confidence < cfg.min_confidence:
                    reason = "low_confidence"
            else:
                name, window = _heuristic(windows, cfg.heuristic)
                method = f"heuristic_{name}" if name else "heuristic_full_scale"
                if window is None:
                    window = [dom.offset, dom.full_scale]
                min_norm, max_norm = to_norm(window[0], dom), to_norm(window[1], dom)
            min_norm, max_norm = _bounded(min_norm, max_norm, upper)
            selection = f.selection
            row = {
                "image_id": image_id, "image_path": str(image_path),
                "image_fingerprint": image.fingerprint(),
                "channel_index": c, "channel_name": f.channel_name, "z": zz, "t": tt,
                **{k: v for k, v in dom.to_dict().items() if k not in ("version", "warnings")},
                "domain_version": dom.version,
                "display_min_raw": to_raw(min_norm, dom), "display_max_raw": to_raw(max_norm, dom),
                "display_min_norm": min_norm, "display_max_norm": max_norm,
                "gamma": GAMMA, "confidence": confidence,
                "abstain": reason is not None, "abstain_reason": reason,
                "method": method, "model_available": model is not None,
                "model_valid": model.valid if model is not None else False,
                "model_version": model.model_version if model is not None else None,
                "checkpoint_id": checkpoint_id if model is not None else None,
                "patch_seed": int(seed),
                "n_patches": len(selection.patches) if selection else 0,
                "patch_level": selection.patch_level if selection else None,
                "overview_level": ctx.overview_level,
                "quota_fallbacks": selection.fallbacks if selection else {},
                "tissue_fraction": ctx.tissue_fraction, "tissue_source": ctx.tissue_source,
                "raw_max_sampled": f.raw_max_sampled,
                "baseline_windows": {k: v for k, v in windows.items() if not k.endswith("_error")},
                "baseline_errors": {k: v for k, v in windows.items() if k.endswith("_error")},
                "warnings": list(dom.warnings) + list(plane_warnings) + flags,
                "config_digest": cfg.digest(), "computed_at": computed_at,
                **versions(),
            }
            if not row["display_min_raw"] < row["display_max_raw"]:
                raise AssertionError(f"window not ordered for channel {c}: {row}")
            rows.append(row)
            if keep_features:
                kept[c] = f
    cached.parent.mkdir(parents=True, exist_ok=True)
    tmp = cached.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rows, default=_json_default), encoding="utf-8")
    tmp.replace(cached)
    return json.loads(json.dumps(rows, default=_json_default)), kept, ctx


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(type(value).__name__)
