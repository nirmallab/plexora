"""Everything the model (or a heuristic) sees about one channel plane, and the
packing of it into the ONNX inputs.

`extract_channel` is the single path from an image to features: prediction,
label pulling and training-example building all call it, so a window learned
from a label was learned from exactly what prediction will show the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from biological_autocontrast import domain as domain_mod
from biological_autocontrast import sampling
from biological_autocontrast.config import FEATURES_VERSION, Config

N_BINS = 256
#: bin 0 is "zero" (below 10^-5.5 of full scale, i.e. under one 16-bit step),
#: the last bin is "at or over full scale"; between them log-spaced.
HIST_EDGES = np.concatenate([[-np.inf], np.logspace(-5.5, 0, N_BINS - 1), [np.inf]])
QUANTILES = (1, 5, 10, 25, 50, 75, 90, 99, 99.5, 99.9)
N_DOM = 16
#: Pixels kept per example so augmentation can recompute histogram and quantiles.
PIXEL_SAMPLE = 65536


@dataclass
class ImageContext:
    """Per (image, z, t): the overview level, the tissue mask and where it came from."""
    image: object
    z: int
    t: int
    cfg: Config
    overview_level: int
    tissue: np.ndarray | None
    tissue_source: str
    tissue_fraction: float
    plane_warnings: list = field(default_factory=list)
    _overviews: dict = field(default_factory=dict)

    @classmethod
    def build(cls, image, z, t, cfg: Config, plane_warnings=()):
        level = image.overview_level(cfg.overview_min_pixels, cfg.overview_max_pixels)
        ctx = cls(image=image, z=z, t=t, cfg=cfg, overview_level=level, tissue=None,
                  tissue_source="none", tissue_fraction=1.0,
                  plane_warnings=list(plane_warnings))
        nuclear = image.nuclear_channel()
        source = f"nuclear:{image.channels[nuclear]}" if nuclear is not None else None
        if nuclear is None:
            nuclear, source = 0, f"channel0:{image.channels[0]} (no nuclear channel by name)"
        mask = sampling.tissue_from(ctx.overview(nuclear))
        if mask is not None:
            ctx.tissue = mask
            ctx.tissue_source = source
            ctx.tissue_fraction = float(mask.mean())
        return ctx

    def overview(self, c):
        if c not in self._overviews:
            self._overviews[c] = self.image.read(c, self.overview_level, None, z=self.z, t=self.t,
                                                 max_pixels=self.cfg.max_plane_pixels)
        return self._overviews[c]


@dataclass
class ChannelFeatures:
    channel_index: int
    channel_name: str
    domain: domain_mod.IntensityDomain
    overview_raw: np.ndarray
    overview_norm: np.ndarray
    finite: np.ndarray
    selection: sampling.Selection | None
    patches: np.ndarray | None  # (N, S, S) canonical
    patch_valid: np.ndarray | None
    pixels: np.ndarray  # canonical tissue sample
    hist: np.ndarray
    quant: np.ndarray
    dom: np.ndarray
    dom_mask: np.ndarray
    raw_max_sampled: float | None
    version: str = FEATURES_VERSION


def histogram(pixels) -> np.ndarray:
    counts, _ = np.histogram(np.asarray(pixels, dtype=np.float64), bins=HIST_EDGES)
    total = counts.sum()
    return (counts / total if total else counts).astype(np.float32)


def quantiles(pixels) -> np.ndarray:
    if not len(pixels):
        return np.full(len(QUANTILES), -1.0, dtype=np.float32)
    q = np.percentile(np.asarray(pixels, dtype=np.float64), QUANTILES)
    return (np.log10(np.maximum(q, 0) + 1e-5) / 5.0).astype(np.float32)


def domain_vector(dom: domain_mod.IntensityDomain, *, tissue_fraction, raw_max_norm,
                  nuclear, level_um, n_patches, n_target):
    """(16 values, 16-mask). A masked entry is unknown, not zero."""
    v = np.zeros(N_DOM, dtype=np.float32)
    m = np.ones(N_DOM, dtype=np.float32)
    if dom.effective_bits:
        v[0] = dom.effective_bits / 16.0
    else:
        m[0] = 0
    v[1] = dom.normalization_confidence
    v[2] = float(dom.storage_dtype.startswith("float"))
    v[3] = float(dom.storage_dtype in ("uint8", "int8"))
    v[4] = float(dom.storage_dtype in ("uint16", "int16"))
    v[5] = float(dom.bits_source in ("ome:SignificantBits", "tiff:BitsPerSample", "user"))
    v[6] = n_patches / max(1, n_target)
    v[7] = dom.saturated_fraction
    v[8] = dom.zero_fraction
    v[9] = dom.finite_fraction
    v[10] = tissue_fraction
    if raw_max_norm is not None:
        v[11] = np.log10(max(raw_max_norm, 0) + 1e-5) / 5.0
    else:
        m[11] = 0
    v[12] = float(nuclear)
    v[13] = float(level_um is not None)
    if level_um:
        v[14] = np.log2(level_um) / 4.0
    else:
        m[14] = 0
    v[15] = dom.nan_fraction + dom.inf_fraction
    return v, m


def tissue_pixels(norm, finite, tissue, rng=None, k=PIXEL_SAMPLE):
    keep = finite if tissue is None or tissue.sum() < 1000 else (finite & tissue)
    values = norm[keep]
    if values.size > k:
        rng = rng or np.random.default_rng(0)
        values = values[rng.choice(values.size, k, replace=False)]
    return values.astype(np.float32)


def extract_channel(ctx: ImageContext, c: int, *, seed: int = 42, assume_bits=None,
                    with_patches: bool = True) -> ChannelFeatures:
    image, cfg = ctx.image, ctx.cfg
    name = image.channels[c]
    raw = ctx.overview(c)
    dom = domain_mod.detect(image, raw, ctx.overview_level, assume_bits=assume_bits)
    norm, finite = domain_mod.normalize(raw, dom)
    selection = patches = valid = None
    raw_max = dom.raw_max
    if with_patches:
        selection = sampling.plan(image, norm, ctx.tissue, ctx.overview_level, cfg, seed, name)
        patches, valid, patch_max = sampling.read_patches(image, c, selection, dom, cfg,
                                                          z=ctx.z, t=ctx.t)
        if patch_max is not None:
            raw_max = patch_max if raw_max is None else max(raw_max, patch_max)
    pixels = tissue_pixels(norm, finite, ctx.tissue, sampling.rng_for(seed, name))
    raw_max_norm = domain_mod.to_norm(raw_max, dom) if raw_max is not None else None
    level_um = (image.level_pixel_size(selection.patch_level)
                if selection is not None else image.level_pixel_size(ctx.overview_level))
    vec, mask = domain_vector(dom, tissue_fraction=ctx.tissue_fraction,
                              raw_max_norm=raw_max_norm, nuclear=image.nuclear_channel() == c,
                              level_um=level_um,
                              n_patches=len(selection.patches) if selection else 0,
                              n_target=cfg.n_patches)
    return ChannelFeatures(channel_index=c, channel_name=name, domain=dom, overview_raw=raw,
                           overview_norm=norm, finite=finite, selection=selection,
                           patches=patches, patch_valid=valid, pixels=pixels,
                           hist=histogram(pixels), quant=quantiles(pixels), dom=vec,
                           dom_mask=mask, raw_max_sampled=raw_max)


def pack(items, n_patches: int, patch_size: int) -> dict:
    """ONNX inputs for a batch. `items`: dicts with patches (N,S,S), categories (N,),
    hist, quant, dom, dom_mask. Missing patches are zero with mask 0."""
    batch = len(items)
    out = {
        "patches": np.zeros((batch, n_patches, 1, patch_size, patch_size), np.float32),
        "patch_cat": np.zeros((batch, n_patches), np.int64),
        "patch_mask": np.zeros((batch, n_patches), np.float32),
        "hist": np.zeros((batch, N_BINS), np.float32),
        "quant": np.zeros((batch, len(QUANTILES)), np.float32),
        "dom": np.zeros((batch, N_DOM), np.float32),
        "dom_mask": np.zeros((batch, N_DOM), np.float32),
    }
    for b, item in enumerate(items):
        patches = item.get("patches")
        if patches is not None and len(patches):
            n = min(n_patches, len(patches))
            out["patches"][b, :n, 0] = patches[:n]
            out["patch_cat"][b, :n] = item["categories"][:n]
            out["patch_mask"][b, :n] = 1.0
        out["hist"][b] = item["hist"]
        out["quant"][b] = item["quant"]
        out["dom"][b] = item["dom"]
        out["dom_mask"][b] = item["dom_mask"]
    return out


def categories_of(selection) -> np.ndarray:
    if selection is None:
        return np.zeros(0, np.int64)
    return np.array([sampling.CATEGORY_INDEX[p["category"]] for p in selection.patches],
                    dtype=np.int64)
