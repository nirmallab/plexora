"""Augmentations, numpy only, applied to an example's patches, pixel sample,
domain vector and targets together, so the label stays true to the image.

- gain g in [0.7, 1.4]: an image g times brighter deserves a window g times
  higher; targets scale with it.
- offset up to 2% of full scale: a raised floor raises black and white.
- Poisson-Gaussian noise: targets unchanged.
- known linear quantisation to 8/12/14 bits: the domain vector is re-derived,
  targets unchanged (they are canonical already).
- flips / 90-degree rotations of patches.

No per-patch rescaling: that would erase exactly the absolute level the
model has to read.
"""

from __future__ import annotations

import numpy as np

from biological_autocontrast import features as feat


def quantise(x, bits):
    top = float(2 ** bits - 1)
    return np.round(np.clip(x, 0.0, 1.0) * top) / top


def requantised_dom(dom, bits):
    dom = dom.copy()
    dom[0] = bits / 16.0
    dom[3] = float(bits <= 8)
    dom[4] = float(8 < bits <= 16)
    dom[5] = 1.0
    return dom


def refresh(item):
    """Histogram, quantiles and the pixel-derived domain entries from the
    (possibly augmented) pixel sample and patches."""
    pixels = item["pixels"]
    item["hist"] = feat.histogram(pixels)
    item["quant"] = feat.quantiles(pixels)
    dom = item["dom"].copy()
    top = max(float(pixels.max()) if pixels.size else 0.0,
              float(item["patches"].max()) if item["patches"].size else 0.0)
    dom[7] = float((pixels >= 1.0).mean()) if pixels.size else 0.0
    dom[8] = float((pixels <= 0.0).mean()) if pixels.size else 0.0
    dom[11] = np.log10(max(top, 0) + 1e-5) / 5.0
    item["dom"] = dom
    return item


def augment(item, rng, *, p_quantise=0.5, clip=True):
    """A new augmented copy of `item` (keys: patches, pixels, dom, dom_mask,
    categories, target_min, target_max)."""
    out = {k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in item.items()}
    gain = float(np.exp(rng.uniform(np.log(0.7), np.log(1.4))))
    offset = float(rng.uniform(0.0, 0.02))
    upper = 1.0 if clip else 2.0
    for key in ("patches", "pixels"):
        x = out[key] * gain + offset
        a, b = rng.uniform(0, 2e-4), rng.uniform(0, 2e-3)
        x = x + rng.normal(0.0, 1.0, x.shape).astype(np.float32) * np.sqrt(a * np.maximum(x, 0) + b * b)
        out[key] = np.clip(x, 0.0, upper).astype(np.float32)
    out["_aug"] = {"gain": gain, "offset": offset}
    out["target_min"] = min(item["target_min"] * gain + offset, upper - 1e-4)
    out["target_max"] = min(item["target_max"] * gain + offset, upper)
    if rng.random() < p_quantise:
        bits = int(rng.choice([8, 12, 14]))
        out["patches"] = quantise(out["patches"], bits).astype(np.float32)
        out["pixels"] = quantise(out["pixels"], bits).astype(np.float32)
        out["dom"] = requantised_dom(out["dom"], bits)
    if out["patches"].size:
        k = int(rng.integers(4))
        out["patches"] = np.rot90(out["patches"], k, axes=(1, 2))
        if rng.random() < 0.5:
            out["patches"] = out["patches"][:, :, ::-1]
        out["patches"] = np.ascontiguousarray(out["patches"])
    return refresh(out)
