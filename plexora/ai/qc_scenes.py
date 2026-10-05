"""Synthetic QC scenes with known truth: artifacts painted into known places.

A scene is a tissue of round cells on glass, imaged in two cycles (DNA_1, CD3,
CD8 | DNA_2, CD20), into which named artifacts are injected. Each injection
returns its truth -- a full-resolution mask, the channels it touches, its
class -- so a scan, a detector, a session and the bench can all be scored
against what is really there. Shared by the tests and `plexora ai bench qc`.

Numpy only; writing a project to disk is the caller's (tests/qc_fixtures.py).
"""

from __future__ import annotations

import numpy as np

CHANNELS = ("DNA_1", "CD3", "CD8", "DNA_2", "CD20")
CYCLE_OF = {"DNA_1": 1, "CD3": 1, "CD8": 1, "DNA_2": 2, "CD20": 2}
BACKGROUND = 60.0
DNA_LEVEL = 3000.0
MARKER_LEVELS = {"CD3": (250.0, 3500.0), "CD8": (200.0, 3000.0), "CD20": (220.0, 2800.0)}

ARTIFACTS = ("blur_local", "saturation", "aggregates", "fold", "dark_region",
             "cycle_dropout", "empty_channel", "illumination")
#: Cycle 2 displaced against cycle 1: everywhere (`global_shift`), or inside
#: one rectangle (`misregistration`). For the Registration Check; kept out of
#: ARTIFACTS so the QC benchmark's scene set is unchanged. Shifts stay under
#: half the cell lattice's period, which a phase correlation would alias.
REGISTRATION_ARTIFACTS = ("global_shift", "misregistration")
#: Every channel out of focus everywhere (a Gaussian of 3 px): the blur an
#: in-image sharp reference cannot see, which Blur QC's global check must.
#: Kept out of ARTIFACTS like the registration ones.
BLUR_ARTIFACTS = ("blur_global",)
#: Foreign objects on the glass, off the tissue (where the Artifact Detector
#: looks for them): a long thin `hair` and a compact `speck`, bright in every
#: channel. Kept out of ARTIFACTS like the registration ones.
DEBRIS_ARTIFACTS = ("hair", "speck")


def _disc_labels(size, grid, spacing, radius, tissue):
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    column = np.minimum((xx // spacing).astype(np.int64), grid - 1)
    row = np.minimum((yy // spacing).astype(np.int64), grid - 1)
    cx, cy = (column + 0.5) * spacing, (row + 0.5) * spacing
    inside = ((xx - cx) ** 2 + (yy - cy) ** 2 <= radius ** 2) & tissue
    labels = np.where(inside, row * grid + column + 1, 0).astype(np.uint32)
    # Renumber the labels that survived the tissue mask, row-major from 1.
    present = np.unique(labels)
    present = present[present > 0]
    lookup = np.zeros(grid * grid + 1, dtype=np.uint32)
    lookup[present] = np.arange(1, present.size + 1, dtype=np.uint32)
    return lookup[labels], present


def _disc(size, cx, cy, r):
    yy, xx = np.ogrid[0:size, 0:size]
    return (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r


def _gaussian(plane, sigma):
    from scipy import ndimage

    return ndimage.gaussian_filter(plane, sigma)


def qc_scene(*, size=1024, grid=40, artifacts=(), seed=0, margin=0.08, shape="square"):
    """(image (C, size, size) uint16, labels, cells, channels, truth).

    `truth = {"regions": [{name, class, channels, mask}], "channels": {name:
    status}, "cells": {cell_id: set(reasons)}}`. The tissue covers the image
    less a margin band (glass), so an edge exists; `shape="round"` makes it a
    disc instead (a TMA core), whose curved rim crosses every map row and
    column at a slant -- an edge no detector may mistake for an artifact."""
    rng = np.random.default_rng(seed)
    channels = CHANNELS
    image = np.full((len(channels), size, size), BACKGROUND, dtype=np.float32)
    image += rng.normal(0, 4, size=image.shape).astype(np.float32)
    band = int(size * margin)
    tissue = np.zeros((size, size), dtype=bool)
    if shape == "round":
        tissue = _disc(size, size / 2.0, size / 2.0, size / 2.0 - band)
    else:
        tissue[band:size - band, band:size - band] = True
    spacing = size / grid
    radius = max(3, int(spacing * 0.38))
    labels, present = _disc_labels(size, grid, spacing, radius, tissue)
    n = int(present.size)
    # Tissue between cells glows a little in every channel (so tissue is
    # separable from glass in every channel, as real tissue is).
    for index in range(len(channels)):
        image[index][tissue] += 80.0
    per_label = np.zeros((len(channels), n + 1), dtype=np.float32)
    cells = []
    for new_id, old in enumerate(present, start=1):
        row, column = divmod(int(old) - 1, grid)
        cx, cy = (column + 0.5) * spacing, (row + 0.5) * spacing
        values = {}
        for name in ("CD3", "CD8", "CD20"):
            low, high = MARKER_LEVELS[name]
            positive = rng.random() < {"CD3": 0.35, "CD8": 0.2, "CD20": 0.25}[name]
            values[name] = float((high if positive else low) * rng.uniform(0.85, 1.15))
        dna = float(DNA_LEVEL * rng.uniform(0.9, 1.1))
        values["DNA_1"] = dna
        values["DNA_2"] = dna * rng.uniform(0.95, 1.05)
        for index, name in enumerate(channels):
            per_label[index, new_id] = values[name]
        cells.append({"id": new_id, "x": float(cx), "y": float(cy), **values})
    inside = labels > 0
    for index in range(len(channels)):
        image[index][inside] = per_label[index][labels[inside]]
    if shape == "round":
        # A core thins out at its rim: the outer tenth of the radius fades to
        # a third of the stain, as a punched core's crushed edge does -- a
        # steep, real intensity step along the tissue boundary, not an artifact.
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
        radius = size / 2.0 - band
        depth = radius - np.hypot(xx - size / 2.0, yy - size / 2.0)
        fade = np.clip(depth / (0.1 * radius), 0.3, 1.0).astype(np.float32)
        for index in range(len(channels)):
            image[index][tissue] = BACKGROUND + (image[index][tissue] - BACKGROUND) * \
                fade[tissue]
    truth = {"regions": [], "channels": {c: "clean" for c in channels}, "cells": {}}
    c = {name: i for i, name in enumerate(channels)}
    lo, hi = band, size - band

    def region(name, klass, chans, mask, pixels=None):
        entry = {"name": name, "class": klass, "channels": list(chans), "mask": mask}
        if pixels is not None:
            # The pixels the artifact itself covers, where they are not the
            # whole region (an aggregate's specks in the field they are in).
            entry["pixels"] = pixels
        truth["regions"].append(entry)

    for artifact in artifacts:
        if artifact == "blur_local":
            cx, cy, r = lo + 0.25 * (hi - lo), lo + 0.3 * (hi - lo), 0.14 * (hi - lo)
            mask = _disc(size, cx, cy, r) & tissue
            for name in ("DNA_2", "CD20"):
                blurred = _gaussian(image[c[name]], 5.0)
                image[c[name]][mask] = blurred[mask]
            region("blur_local", "out_of_focus", ("DNA_2", "CD20"), mask)
        elif artifact == "saturation":
            x0, y0, w = int(lo + 0.6 * (hi - lo)), int(lo + 0.15 * (hi - lo)), int(0.1 * size)
            mask = np.zeros((size, size), dtype=bool)
            mask[y0:y0 + w, x0:x0 + w] = True
            image[c["CD3"]][mask] = 65535.0
            region("saturation", "saturation_or_clipping", ("CD3",), mask)
        elif artifact == "aggregates":
            cx, cy, r = lo + 0.72 * (hi - lo), lo + 0.72 * (hi - lo), 0.12 * (hi - lo)
            area = _disc(size, cx, cy, r) & tissue
            plane = image[c["CD8"]]
            ys, xs = np.nonzero(area)
            pick = rng.choice(ys.size, size=min(60, ys.size), replace=False)
            specks = np.zeros((size, size), dtype=bool)
            for i in pick:
                blob = _disc(size, xs[i], ys[i], rng.integers(2, 4))
                plane[blob] = 60000.0
                specks |= blob
            region("aggregates", "antibody_aggregate", ("CD8",), area, pixels=specks)
        elif artifact == "fold":
            yy, xx = np.mgrid[0:size, 0:size]
            line = np.abs((yy - lo) - 0.9 * (xx - lo) - 0.05 * (hi - lo)) < 0.035 * size
            mask = line & tissue & (xx > lo + 0.4 * (hi - lo))
            for index in range(len(channels)):
                boosted = _gaussian(image[index], 2.0) * 2.2
                image[index][mask] = boosted[mask]
            region("fold", "tissue_fold", channels, mask)
        elif artifact == "dark_region":
            x0, y0, w, h = int(lo + 0.08 * (hi - lo)), int(lo + 0.7 * (hi - lo)), \
                int(0.18 * size), int(0.12 * size)
            mask = np.zeros((size, size), dtype=bool)
            mask[y0:y0 + h, x0:x0 + w] = True
            mask &= tissue
            for index in range(len(channels)):
                image[index][mask] = BACKGROUND + (image[index][mask] - BACKGROUND) * 0.1
            region("dark_region", "tissue_damage_or_detachment", channels, mask)
        elif artifact == "cycle_dropout":
            x0, y0, w = int(lo + 0.45 * (hi - lo)), int(lo + 0.45 * (hi - lo)), int(0.15 * size)
            mask = np.zeros((size, size), dtype=bool)
            mask[y0:y0 + w, x0:x0 + w] = True
            mask &= tissue
            for name in ("DNA_2", "CD20"):
                image[c[name]][mask] = BACKGROUND + rng.normal(0, 4, size=int(mask.sum()))
            region("cycle_dropout", "cycle_specific_tissue_loss", ("DNA_2", "CD20"), mask)
            for cell in cells:
                if mask[int(cell["y"]), int(cell["x"])]:
                    truth["cells"].setdefault(cell["id"], set()).add("cycle_loss")
                    cell["DNA_2"] = BACKGROUND
        elif artifact == "global_shift":
            for name in ("DNA_2", "CD20"):
                image[c[name]] = np.roll(image[c[name]], (3, 4), axis=(0, 1))
            region("global_shift", "cross_cycle_registration_error", ("DNA_2", "CD20"),
                   tissue.copy())
        elif artifact == "misregistration":
            x0, y0 = int(lo + 0.2 * (hi - lo)), int(lo + 0.25 * (hi - lo))
            w, h = int(0.4 * (hi - lo)), int(0.35 * (hi - lo))
            mask = np.zeros((size, size), dtype=bool)
            mask[y0:y0 + h, x0:x0 + w] = True
            for name in ("DNA_2", "CD20"):
                moved = np.roll(image[c[name]], (8, 5), axis=(0, 1))
                image[c[name]][mask] = moved[mask]
            region("misregistration", "cross_cycle_registration_error", ("DNA_2", "CD20"),
                   mask & tissue)
        elif artifact == "empty_channel":
            image[c["CD20"]] = BACKGROUND + rng.normal(0, 4, size=(size, size))
            truth["channels"]["CD20"] = "failed"
        elif artifact == "illumination":
            ramp = np.linspace(0.4, 1.6, size, dtype=np.float32)[None, :]
            image[c["CD20"]] = BACKGROUND + (image[c["CD20"]] - BACKGROUND) * ramp
            region("illumination", "illumination_or_shading", ("CD20",),
                   tissue.copy())
        elif artifact == "hair":
            # A 3 px wide line along the bottom glass band, far enough from
            # the tissue (> 40 px) to be on clear glass.
            yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
            y_mid = size - 0.27 * band
            x0, x1 = 0.28 * size, 0.62 * size
            along = (xx >= x0) & (xx <= x1)
            slope = 0.02
            distance = np.abs(yy - (y_mid + slope * (xx - x0)))
            weight = np.clip(2.0 - distance, 0.0, 1.0) * along
            mask = weight > 0.5
            for index in range(len(channels)):
                image[index] = image[index] * (1 - weight) + 1800.0 * weight
            region("hair", "debris_or_foreign_object", channels, mask)
        elif artifact == "speck":
            mask = _disc(size, 0.82 * size, size - 0.22 * band, 9)
            for index in range(len(channels)):
                image[index][mask] = 5000.0
            region("speck", "debris_or_foreign_object", channels, mask)
        elif artifact == "blur_global":
            for index in range(len(channels)):
                image[index] = _gaussian(image[index], 3.0)
            truth["global_blur"] = True
        else:
            raise ValueError(f"unknown artifact {artifact!r}")
    for entry in truth["regions"]:
        for name in entry["channels"]:
            if truth["channels"][name] == "clean":
                truth["channels"][name] = "flagged"
    return (np.clip(image, 0, 65535).astype(np.uint16), labels, cells, channels, truth)
