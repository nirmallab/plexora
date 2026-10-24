"""Stratified patch selection: 24 small views of a channel chosen so the model
sees its brightest structures, its mid-tones, its background, its texture and
an unbiased draw -- not 24 random squares that are mostly glass.

Candidates are footprint-sized cells of the overview (on two half-offset
grids); each is scored on the canonical overview, then filled quota by quota.
Patches are read at the level nearest `target_um_per_px` and normalised by the
channel's one domain transform, never rescaled one by one.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field

import numpy as np

from biological_autocontrast.config import SAMPLING_VERSION, Config
from biological_autocontrast.domain import normalize

CATEGORIES = ("high", "intermediate", "background", "texture", "random")
CATEGORY_INDEX = {name: i for i, name in enumerate(CATEGORIES)}


def rng_for(seed: int, channel_name: str) -> np.random.Generator:
    return np.random.default_rng([int(seed), zlib.crc32(str(channel_name).encode())])


def patch_level(image, cfg: Config, overview_level: int) -> int:
    """The level nearest `target_um_per_px`, never coarser than the overview;
    without a pixel size, two levels finer than the overview (>= 4x its resolution)."""
    if image.pixel_size_um:
        best, best_err = 0, None
        for level in range(overview_level + 1):
            size = image.level_pixel_size(level)
            err = abs(np.log(size / cfg.target_um_per_px))
            if best_err is None or err < best_err - 1e-9:
                best, best_err = level, err
        return best
    return max(0, overview_level - 2)


def tissue_from(nuclear_raw):
    """plexora.agent.evidence.image_qc.tissue_mask on the nuclear overview."""
    if nuclear_raw is None:
        return None
    from plexora.agent.evidence.image_qc import tissue_mask

    plane = np.nan_to_num(np.asarray(nuclear_raw, dtype=np.float32), nan=0.0,
                          posinf=0.0, neginf=0.0)
    return tissue_mask(plane)


@dataclass
class Selection:
    patch_level: int
    overview_level: int
    footprint: int  # overview pixels
    patches: list  # dicts: category, cx, cy (overview px), box (x, y, w, h at patch_level)
    fallbacks: dict = field(default_factory=dict)
    n_candidates: int = 0
    version: str = SAMPLING_VERSION


def _cell_stats(norm, tissue, size, oy, ox):
    """Per-cell stats on a grid of `size` cells starting at (oy, ox)."""
    height, width = norm.shape
    ny, nx = (height - oy) // size, (width - ox) // size
    if ny < 1 or nx < 1:
        return None
    crop = norm[oy:oy + ny * size, ox:ox + nx * size]
    blocks = crop.reshape(ny, size, nx, size).transpose(0, 2, 1, 3).reshape(ny, nx, -1)
    logged = np.log10(blocks + 1e-4)
    from scipy.ndimage import laplace

    lap = laplace(np.log10(crop + 1e-4))
    lap_blocks = lap.reshape(ny, size, nx, size).transpose(0, 2, 1, 3).reshape(ny, nx, -1)
    if tissue is not None:
        tcrop = tissue[oy:oy + ny * size, ox:ox + nx * size]
        tfrac = tcrop.reshape(ny, size, nx, size).mean(axis=(1, 3))
    else:
        tfrac = np.ones((ny, nx))
    ys, xs = np.meshgrid(np.arange(ny), np.arange(nx), indexing="ij")
    return {
        "cy": (oy + ys * size + size / 2).ravel(), "cx": (ox + xs * size + size / 2).ravel(),
        "mean": blocks.mean(axis=2).ravel(),
        "p90": np.percentile(blocks, 90, axis=2).ravel(),
        "logstd": logged.std(axis=2).ravel(),
        "texture": lap_blocks.var(axis=2).ravel(),
        "tissue": tfrac.ravel(),
    }


def score_grid(norm, tissue, footprint: int) -> dict:
    """Candidate cells (two grids offset by half a footprint) with their scores."""
    height, width = norm.shape
    size = int(max(1, min(footprint, height, width)))
    parts = [s for s in (_cell_stats(norm, tissue, size, 0, 0),
                         _cell_stats(norm, tissue, size, size // 2, size // 2)) if s]
    if not parts:
        return {k: np.zeros(0) for k in ("cy", "cx", "mean", "p90", "logstd", "texture", "tissue")}
    return {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}


def select_patches(scores: dict, footprint: int, cfg: Config, seed: int, channel_name: str):
    """(patches, fallbacks). Deterministic for a (seed, channel name)."""
    rng = rng_for(seed, channel_name)
    n = len(scores["cx"])
    if n == 0:
        return [], {"no_candidates": cfg.n_patches}
    on_tissue = scores["tissue"] >= 0.5
    if on_tissue.sum() < cfg.n_patches:
        on_tissue = scores["tissue"] > 0
    if on_tissue.sum() < cfg.n_patches:
        on_tissue = np.ones(n, dtype=bool)
    pool = np.flatnonzero(on_tissue)
    p90 = scores["p90"][pool]
    mean = scores["mean"][pool]
    texture = scores["texture"][pool]
    q50, q90 = np.percentile(p90, [50, 90])
    pools = {
        "high": pool[p90 >= q90],
        "intermediate": pool[(p90 >= q50) & (p90 < q90)],
        "background": pool[mean <= np.percentile(mean, 25)],
        "texture": pool[texture >= np.percentile(texture, 80)],
        "random": pool,
    }
    chosen, fallbacks = [], {}
    taken = set()
    spacing = cfg.min_distance * footprint

    def far_enough(index, gap):
        for other in chosen:
            if max(abs(scores["cx"][index] - scores["cx"][other["i"]]),
                   abs(scores["cy"][index] - scores["cy"][other["i"]])) < gap:
                return False
        return True

    def fill(category, candidates, quota, gap):
        got = 0
        for index in rng.permutation(candidates):
            if got >= quota:
                break
            if index in taken or not far_enough(index, gap):
                continue
            taken.add(int(index))
            chosen.append({"i": int(index), "category": category})
            got += 1
        return got

    for category in CATEGORIES:
        quota = int(cfg.quotas.get(category, 0))
        got = fill(category, pools[category], quota, spacing)
        if got < quota:
            # Short: take the rest from anywhere on tissue, still spaced, and say so.
            extra = fill(category, pool, quota - got, spacing)
            fallbacks[category] = quota - got
            if got + extra < quota:
                more = fill(category, pool, quota - got - extra, spacing / 2)
                if more:
                    fallbacks["min_distance_relaxed"] = fallbacks.get("min_distance_relaxed", 0) + more
    short = cfg.n_patches - len(chosen)
    if short > 0:
        fallbacks["short"] = short
    patches = [{"category": c["category"], "cx": float(scores["cx"][c["i"]]),
                "cy": float(scores["cy"][c["i"]]), "p90": float(scores["p90"][c["i"]]),
                "mean": float(scores["mean"][c["i"]]), "tissue": float(scores["tissue"][c["i"]])}
               for c in chosen[:cfg.n_patches]]
    return patches, {k: int(v) for k, v in fallbacks.items() if v}


def plan(image, overview_norm, tissue, overview_level, cfg: Config, seed: int,
         channel_name: str) -> Selection:
    level = patch_level(image, cfg, overview_level)
    ratio = image.downsample(level) / image.downsample(overview_level)
    footprint = int(max(1, round(cfg.patch_size * ratio)))
    scores = score_grid(overview_norm, tissue, footprint)
    patches, fallbacks = select_patches(scores, footprint, cfg, seed, channel_name)
    scale = image.downsample(overview_level) / image.downsample(level)
    half = cfg.patch_size // 2
    for patch in patches:
        x = int(round(patch["cx"] * scale)) - half
        y = int(round(patch["cy"] * scale)) - half
        patch["box"] = [x, y, cfg.patch_size, cfg.patch_size]
    return Selection(patch_level=level, overview_level=overview_level, footprint=footprint,
                     patches=patches, fallbacks=fallbacks, n_candidates=len(scores["cx"]))


def read_patches(image, c, selection: Selection, domain, cfg: Config, z=0, t=0):
    """(patches (N, S, S) float32 canonical, valid (N, S, S) bool, raw_max float|None)."""
    size = cfg.patch_size
    out = np.zeros((len(selection.patches), size, size), dtype=np.float32)
    valid = np.zeros(out.shape, dtype=bool)
    height, width = image.level_shape(selection.patch_level)
    raw_max = None
    for i, patch in enumerate(selection.patches):
        x, y, w, h = patch["box"]
        # Shift inside the level rather than pad, when the level is big enough.
        x = int(min(max(0, x), max(0, width - w)))
        y = int(min(max(0, y), max(0, height - h)))
        patch["box"] = [x, y, w, h]
        raw = image.read(c, selection.patch_level, (x, y, w, h), z=z, t=t)
        if raw.size:
            finite = raw[np.isfinite(raw)] if raw.dtype.kind == "f" else raw
            if finite.size:
                top = float(finite.max())
                raw_max = top if raw_max is None else max(raw_max, top)
        norm, mask = normalize(raw, domain)
        out[i, :norm.shape[0], :norm.shape[1]] = norm
        valid[i, :norm.shape[0], :norm.shape[1]] = mask
    return out, valid, raw_max
