"""Training examples, materialised in the plexora environment.

Each label becomes one compressed npz (patches, a canonical tissue-pixel
sample for re-deriving the histogram after augmentation, domain vector,
targets) plus a line in `examples.json` (identity, specimen, targets and the
baseline windows in canonical units). Training then needs only numpy and
torch -- no image reader, no plexora -- and evaluation needs no image reads.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from biological_autocontrast import baselines, features as feat
from biological_autocontrast.config import Config, home, versions
from biological_autocontrast.domain import to_norm
from biological_autocontrast.io import open_image

REGRESSION_DECISIONS = ("accept", "edit")


def _key(fingerprint, channel, z, t, seed, cfg, assume_bits=None) -> str:
    blob = json.dumps({"f": fingerprint, "c": int(channel), "z": int(z), "t": int(t),
                       "seed": int(seed), "cfg": cfg.digest(), "bits": assume_bits,
                       **versions()}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def _canonical(window, dom):
    if not window:
        return None
    return [to_norm(window[0], dom), to_norm(window[1], dom)]


def build(index=None, out=None, *, seed=42, cfg=None) -> dict:
    import pandas as pd

    cfg = cfg or Config()
    index = Path(index) if index else home() / "labels_index.parquet"
    out = Path(out) if out else home() / "examples"
    out.mkdir(parents=True, exist_ok=True)
    frame = pd.read_parquet(index)
    if frame.empty:
        raise ValueError(f"{index} holds no labels; run `labels pull` and `labels index` first")
    frame = frame[frame["decision"].isin(REGRESSION_DECISIONS + ("no_signal",))]
    records, made, reused, stale = [], 0, 0, []
    for (path, z, t), group in frame.groupby(["image_path", "z", "t"], sort=True):
        with open_image(path) as image:
            fingerprint = image.fingerprint()
            current = group[group["image_fingerprint"] == fingerprint]
            if len(current) < len(group):
                stale.append({"image_path": path, "labels": int(len(group) - len(current)),
                              "reason": "image changed since it was labelled"})
            if current.empty:
                continue
            ctx = feat.ImageContext.build(image, int(z), int(t), cfg)
            for row in current.itertuples():
                c = int(row.channel_index)
                key = _key(fingerprint, c, z, t, seed, cfg)
                file = out / f"{key}.npz"
                f = feat.extract_channel(ctx, c, seed=seed)
                dom = f.domain
                if not file.exists():
                    np.savez_compressed(
                        file, patches=f.patches.astype(np.float32),
                        categories=feat.categories_of(f.selection), pixels=f.pixels,
                        hist=f.hist, quant=f.quant, dom=f.dom, dom_mask=f.dom_mask)
                    made += 1
                else:
                    reused += 1
                nuclear = image.nuclear_channel() == c
                windows = baselines.all_windows(f.overview_raw, nuclear)
                records.append({
                    "id": key, "file": file.name, "label_id": row.label_id,
                    "image_id": row.image_id, "specimen": str(getattr(row, "specimen", "") or row.image_id),
                    "image_path": path, "channel_index": c, "channel_name": row.channel_name,
                    "z": int(z), "t": int(t), "nuclear": bool(nuclear),
                    "decision": row.decision, "weight": float(row.weight),
                    "target_min_norm": float(row.display_min_norm),
                    "target_max_norm": float(row.display_max_norm),
                    "abstain_target": row.decision == "no_signal",
                    "storage_dtype": dom.storage_dtype, "effective_bits": dom.effective_bits,
                    "bits_source": dom.bits_source, "offset": dom.offset,
                    "full_scale": dom.full_scale,
                    "normalization_method": dom.normalization_method,
                    "baselines_norm": {k: _canonical(v, dom) for k, v in windows.items()
                                       if not k.endswith("_error")},
                    "n_patches": len(f.selection.patches), "tissue_fraction": ctx.tissue_fraction,
                    "patch_seed": seed, **{k: str(v) for k, v in getattr(row, "_asdict")().items()
                                           if k in ("tissue", "study", "instrument", "batch", "patient")},
                })
    meta = {"versions": versions(), "config": cfg.to_dict(), "seed": seed,
            "n_patches": cfg.n_patches, "patch_size": cfg.patch_size,
            "examples": records}
    (out / "examples.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return {"examples": len(records), "written": made, "reused": reused,
            "specimens": len({r["specimen"] for r in records}), "stale": stale,
            "folder": str(out)}


def load(folder=None):
    """(meta dict, {id: npz arrays}) -- numpy only, usable from the training env."""
    folder = Path(folder) if folder else home() / "examples"
    meta = json.loads((folder / "examples.json").read_text(encoding="utf-8"))
    arrays = {}
    for record in meta["examples"]:
        with np.load(folder / record["file"]) as data:
            arrays[record["id"]] = {k: data[k] for k in data.files}
    return meta, arrays
