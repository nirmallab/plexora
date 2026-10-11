"""Quality checks that only a stack can fail: focus by plane, fading with depth, bad planes.

A 2-D QC check asks where in the image something went wrong; these ask WHICH
PLANES. A confocal stack drifts out of focus at its ends, loses signal with
depth as light scatters, and now and then carries an empty plane, a plane
written twice, or one exposed differently from its neighbours. None of that
is visible on the one plane a 2-D tool looks at, and all of it decides which
planes a measurement may use.

Each check is deterministic over per-plane statistics of one channel (read at
a coarse level -- it compares planes with each other, it does not measure
cells) and returns COMPACT findings: a plane range and a reason, never a
polygon per plane. Parameters live in `PARAMS`, one dict, so a threshold is
never restated in prose.

    focus_profile      planes much less sharp than the sharpest (runs of them)
    axial_attenuation  signal over background falling with depth (fit, reported)
    missing_slices     empty planes, duplicated planes, outlier planes

and three that need more than one channel's planes:

    z_registration_drift  a cycle's nuclear stain sitting deeper or shallower
                          than the reference cycle's (3-D phase correlation)
    stitching_seams       straight edges at the same column (or row) of every
                          plane: tile boundaries of a stitched acquisition
    segmentation_3d       objects of a 3-D mask that are not one whole cell: flat
                          (one plane), cut off by the stack's ends, much taller
                          than wide (cells stacked and merged), split between
                          neighbouring planes, or far from the usual volume
"""

from __future__ import annotations

import numpy as np

#: Every cut-point the checks use, in one place.
PARAMS = {
    # A plane is out of focus when its sharpness is below this share of the
    # sharpest plane's, over at least `focus_min_run` consecutive planes.
    "focus_deficit": 0.35,
    "focus_min_run": 2,
    # Attenuation is flagged when the last plane keeps less than this share of
    # the brightest plane's signal over background.
    "attenuation_floor": 0.3,
    "attenuation_min_planes": 6,
    # Duplicate: a plane identical to the previous one on at least this share
    # of the pixels either has signal on. A plane written twice is
    # bit-identical; a small mean difference would also flag two sparse planes
    # that are mostly zero, and correlation two neighbours of a sharp, quiet one.
    "duplicate_same": 0.999,
    # Outlier: a plane's median differs from its neighbours' by more than this
    # many robust standard deviations of the plane-to-plane differences.
    "outlier_z": 4.0,
    # Faint: a plane whose signal over background (p99 - p05) is below this
    # share of the brightest plane's -- beyond the tissue, usually. Such planes
    # are reported once, as faint, and judged by no other plane check (a
    # blank plane is neither blurred nor a repeat of the blank one before it).
    "faint_share": 0.05,
    # A cycle drifted in z when its nuclear stain sits at least this many
    # planes from the reference cycle's, with a phase-correlation peak at
    # least `drift_min_peak` (a weaker peak is no evidence either way).
    "drift_planes": 0.5,
    "drift_min_peak": 0.05,
    # A seam: a column (row) whose step to its neighbour is at least
    # `seam_ratio` times the median column's, on at least `seam_planes` of
    # the planes; and at least `seam_min` such columns before any is reported
    # (one edge is tissue, a grid of them is tiling).
    "seam_ratio": 3.0,
    "seam_planes": 0.5,
    "seam_min": 2,
    # Objects: taller than `merged_ratio` times their equivalent diameter
    # (two stacked cells), volume beyond `volume_z` robust SDs of the log
    # volumes, and a pair in neighbouring planes overlapping by at least
    # `split_iou` in XY (one cell cut between planes).
    "merged_ratio": 2.0,
    "volume_z": 4.0,
    "split_iou": 0.6,
}


def plane_stats(planes):
    """Per-plane statistics of a list of 2-D arrays (one channel, z order)."""
    rows = []
    for z, plane in enumerate(planes):
        values = np.asarray(plane, dtype=np.float64)
        gy = values[2:, 1:-1] - values[:-2, 1:-1]
        gx = values[1:-1, 2:] - values[1:-1, :-2]
        focus = float(np.mean(gx * gx + gy * gy)) if gx.size else 0.0
        p05, median, p99 = (float(v) for v in np.percentile(values, (5, 50, 99))) \
            if values.size else (0.0, 0.0, 0.0)
        rows.append({"z": z, "focus": focus, "median": median, "p99": p99, "p05": p05,
                     "max": float(values.max()) if values.size else 0.0})
    return rows


def _runs(flags):
    """[(start, end)] inclusive runs of True."""
    runs, start = [], None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        if not flag and start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(flags) - 1))
    return runs


def _faint(stats, params):
    """Which planes carry almost no signal (see PARAMS["faint_share"])."""
    signal = np.array([max(0.0, row["p99"] - row["p05"]) for row in stats], dtype=np.float64)
    if not len(signal) or signal.max() <= 0:
        return np.ones(len(signal), dtype=bool)
    return signal < params["faint_share"] * signal.max()


def focus_profile(stats, channel=None, params=PARAMS):
    focus = np.array([row["focus"] for row in stats], dtype=np.float64)
    if not len(focus) or focus.max() <= 0:
        return []
    share = focus / focus.max()
    flags = (share < params["focus_deficit"]) & ~_faint(stats, params)
    findings = []
    for z0, z1 in _runs(flags):
        if z1 - z0 + 1 < params["focus_min_run"]:
            continue
        findings.append({
            "check": "focus_profile", "channel": channel, "z0": z0, "z1": z1,
            "severity": "warn",
            "reason": f"planes {z0}–{z1} are at most {share[z0:z1 + 1].max():.0%} as sharp "
                      "as the sharpest plane",
            "evidence": {"relative_sharpness": [round(float(v), 3)
                                                for v in share[z0:z1 + 1]]}})
    return findings


def axial_attenuation(stats, channel=None, params=PARAMS):
    signal = np.array([max(0.0, row["p99"] - row["p05"]) for row in stats], dtype=np.float64)
    if len(signal) < params["attenuation_min_planes"] or signal.max() <= 0:
        return []
    peak = int(np.argmax(signal))
    last = len(signal) - 1
    kept = float(signal[last] / signal[peak])
    if peak == last or kept >= params["attenuation_floor"]:
        return []
    below = np.nonzero(signal[peak:] < params["attenuation_floor"] * signal[peak])[0]
    z0 = peak + int(below[0]) if len(below) else last
    return [{
        "check": "axial_attenuation", "channel": channel, "z0": z0, "z1": last,
        "severity": "warn",
        "reason": f"signal over background falls to {kept:.0%} of its brightest plane "
                  f"(plane {peak}) by the last plane; deeper planes are dimmer for optical "
                  "reasons, not necessarily biological ones",
        "evidence": {"peak_z": peak, "last_over_peak": round(kept, 3)}}]


def _span(z0, z1):
    return f"plane {z0}" if z0 == z1 else f"planes {z0}–{z1}"


def missing_slices(planes, stats, channel=None, params=PARAMS):
    """Empty and repeated planes -- each run of them one finding -- and planes
    whose level is far from their neighbours'."""
    findings = []
    empty = [row["max"] <= 0 for row in stats]
    for z0, z1 in _runs(empty):
        at_end = z0 == 0 or z1 == len(stats) - 1
        findings.append({"check": "missing_slices", "channel": channel, "z0": z0, "z1": z1,
                         "severity": "fail", "kind": "empty",
                         "reason": f"{_span(z0, z1)} {'is' if z0 == z1 else 'are'} empty "
                                   "(every pixel zero)"
                                   + ("; at the end of the stack, likely beyond the tissue"
                                      if at_end else "")})
    faint = _faint(stats, params)
    for z0, z1 in _runs([bool(f) and not e for f, e in zip(faint, empty)]):
        findings.append({"check": "missing_slices", "channel": channel, "z0": z0, "z1": z1,
                         "severity": "warn", "kind": "faint",
                         "reason": f"{_span(z0, z1)} {'carries' if z0 == z1 else 'carry'} "
                                   "almost no signal"
                                   + ("; at the end of the stack, likely beyond the tissue"
                                      if z0 == 0 or z1 == len(stats) - 1 else "")})
    repeats = [False] * len(planes)
    for z in range(1, len(planes)):
        if faint[z] or faint[z - 1]:
            continue
        a, b = np.asarray(planes[z - 1]), np.asarray(planes[z])
        signal = (a != 0) | (b != 0)
        if signal.any():
            repeats[z] = float(np.mean(a[signal] == b[signal])) >= params["duplicate_same"]
    for z0, z1 in _runs(repeats):
        findings.append({"check": "missing_slices", "channel": channel, "z0": z0, "z1": z1,
                         "severity": "fail", "kind": "duplicate",
                         "reason": f"{_span(z0, z1)} {'repeats' if z0 == z1 else 'repeat'} "
                                   f"plane {z0 - 1} pixel for pixel"})
    medians = np.array([row["median"] for row in stats], dtype=np.float64)
    if len(medians) >= 5:
        neighbours = np.array([np.median(np.concatenate([medians[max(0, z - 2):z],
                                                         medians[z + 1:z + 3]]))
                               for z in range(len(medians))])
        diff = medians - neighbours
        # Never below one grey level, nor a fiftieth of the levels' own range:
        # a stack whose planes all share one level (zero spread) would make
        # any difference infinitely many SDs.
        spread = float(np.ptp(medians[~faint])) if (~faint).any() else 0.0
        scale = max(1.4826 * float(np.median(np.abs(np.diff(medians)))), spread / 50.0, 1.0)
        for z in np.nonzero(np.abs(diff) > params["outlier_z"] * scale)[0]:
            # A plane beside a faint or empty one is where the tissue starts or
            # ends: its level is bound to differ from its neighbours'.
            near = faint[max(0, int(z) - 2):int(z) + 3]
            if near.any() or any(f["z0"] <= int(z) <= f["z1"] for f in findings):
                continue
            findings.append({"check": "missing_slices", "channel": channel, "z0": int(z),
                             "z1": int(z), "severity": "warn", "kind": "outlier",
                             "reason": f"plane {int(z)}'s level differs from its neighbours' "
                                       f"by {abs(diff[z]) / scale:.1f} robust SDs"})
    return findings


CHECKS = ("focus_profile", "axial_attenuation", "missing_slices")
def run_checks(planes, *, channel=None, checks=CHECKS, params=PARAMS):
    """All requested checks over one channel's planes; `(findings, stats)`."""
    stats = plane_stats(planes)
    findings = []
    if "focus_profile" in checks:
        findings += focus_profile(stats, channel, params)
    if "axial_attenuation" in checks:
        findings += axial_attenuation(stats, channel, params)
    if "missing_slices" in checks:
        findings += missing_slices(planes, stats, channel, params)
    return findings, stats


#: The checks that read more than one channel's planes (or the object table).
STACK_CHECKS = ("z_registration_drift", "stitching_seams", "segmentation_3d")


# -- a cycle drifting in z -----------------------------------------------------


def _subpixel(values, index):
    """The peak of a sampled curve near `index`, by a parabola through it and
    its two neighbours (circular)."""
    n = len(values)
    left, centre, right = values[(index - 1) % n], values[index], values[(index + 1) % n]
    denominator = left - 2 * centre + right
    offset = 0.5 * (left - right) / denominator if denominator else 0.0
    return index + float(np.clip(offset, -0.5, 0.5))


def phase_shift_3d(reference, moving):
    """`(dz, dy, dx, peak)`: how far `moving` (Z, Y, X) sits from `reference`
    -- positive dz: deeper -- by normalised cross-power phase correlation, with
    a Hann window in every axis and a sub-plane fit of the peak."""
    a = np.asarray(reference, dtype=np.float64)
    b = np.asarray(moving, dtype=np.float64)
    if a.shape != b.shape or a.ndim != 3:
        raise ValueError("phase correlation needs two volumes of the same shape")
    window = 1.0
    for axis, size in enumerate(a.shape):
        shape = [1, 1, 1]
        shape[axis] = size
        window = window * np.hanning(size).reshape(shape) if size > 2 else window
    fa = np.fft.fftn((a - a.mean()) * window)
    fb = np.fft.fftn((b - b.mean()) * window)
    cross = fb * np.conj(fa)
    cross /= np.abs(cross) + 1e-12
    surface = np.real(np.fft.ifftn(cross))
    peak_index = np.unravel_index(int(np.argmax(surface)), surface.shape)
    shifts = []
    for axis, index in enumerate(peak_index):
        line = np.moveaxis(surface, axis, 0)[(slice(None),) + tuple(
            peak_index[k] for k in range(3) if k != axis)]
        value = _subpixel(line, int(index))
        size = surface.shape[axis]
        shifts.append(value - size if value > size / 2 else value)
    return shifts[0], shifts[1], shifts[2], float(surface[peak_index])


def z_registration_drift(reference, cycles, *, reference_cycle=0, params=PARAMS):
    """Findings for each cycle whose nuclear volume sits off the reference
    cycle's in z. `reference` is (Z, Y, X); `cycles` is
    `[(cycle, channels, volume)]` with each volume the same shape."""
    findings = []
    for cycle, channels, volume in cycles:
        dz, dy, dx, peak = phase_shift_3d(reference, volume)
        if peak < params["drift_min_peak"] or abs(dz) < params["drift_planes"]:
            continue
        depth = int(np.asarray(reference).shape[0])
        findings.append({
            "check": "z_registration_drift", "cycle": int(cycle), "channels": list(channels),
            "channel": None, "z0": 0, "z1": depth - 1, "severity": "warn",
            "shift": {"dz": round(dz, 2), "dy": round(dy, 2), "dx": round(dx, 2)},
            "reason": f"cycle {cycle}'s nuclear stain sits {abs(dz):.1f} plane"
                      f"{'' if abs(dz) == 1 else 's'} {'deeper' if dz > 0 else 'shallower'} "
                      f"than cycle {reference_cycle}'s: its markers are read at other depths "
                      "than the reference's",
            "evidence": {"peak": round(peak, 3)}})
    return findings


# -- seams of a stitched acquisition ------------------------------------------------


def _seam_columns(planes, axis, params):
    """Columns (axis=1: the step from one column to the next, averaged down
    the rows) or rows (axis=0) whose step is a seam on enough planes;
    `(positions, share_of_planes)`."""
    votes = None
    for plane in planes:
        values = np.asarray(plane, dtype=np.float64)
        if values.shape[0] < 3 or values.shape[1] < 3:
            return [], 0.0
        step = np.abs(np.diff(values, axis=axis)).mean(axis=1 - axis)
        typical = float(np.median(step)) or 1e-9
        hit = step > params["seam_ratio"] * typical
        votes = hit.astype(int) if votes is None else votes + hit
    if votes is None:
        return [], 0.0
    share = votes / float(len(planes))
    positions = [int(i) for i in np.nonzero(share >= params["seam_planes"])[0]]
    # One seam is a run of neighbouring columns: keep the middle of each run.
    merged = []
    for position in positions:
        if merged and position - merged[-1][-1] <= 2:
            merged[-1].append(position)
        else:
            merged.append([position])
    centres = [int(round(np.mean(run))) for run in merged]
    return centres, float(share.max()) if share.size else 0.0


def stitching_seams(planes, *, channel=None, scale=1, params=PARAMS):
    """Findings for straight edges at fixed columns or rows through the
    stack, at full-resolution positions (`scale`: pixels of the level read)."""
    findings = []
    for axis, name in ((1, "x"), (0, "y")):
        positions, share = _seam_columns(planes, axis, params)
        if len(positions) < params["seam_min"]:
            continue
        gaps = np.diff(positions)
        regular = len(gaps) >= 1 and float(np.std(gaps)) <= 0.1 * float(np.mean(gaps))
        full = [int((p + 0.5) * scale) for p in positions]
        findings.append({
            "check": "stitching_seams", "channel": channel, "axis": name,
            "positions": full, "z0": 0, "z1": len(planes) - 1, "severity": "warn",
            "regular": bool(regular),
            "reason": f"{len(full)} straight edges along {'x' if name == 'y' else 'y'} at "
                      f"fixed {name} positions on most planes"
                      + (f", every {int(np.mean(gaps) * scale)} px: tile seams"
                         if regular else ": likely tile seams"),
            "evidence": {"share_of_planes": round(share, 2)}})
    return findings


# -- objects that are not one whole cell ----------------------------------------------


def segmentation_3d(frame, *, depth, params=PARAMS):
    """`(findings, flagged)` for a measured 3-D object table (`objects_v1`):
    `flagged` is `{label: [reasons]}`, reasons among flat, clipped_z,
    merged_z, split_z, volume_outlier; `findings` one per reason with its
    count."""
    def column(name, default=None):
        return frame[name].to_numpy() if name in frame.columns else default

    labels = column("label").astype(np.int64)
    n_planes = column("n_planes")
    z0, z1 = column("bbox_z0"), column("bbox_z1")
    flagged = {}

    def flag(rows, reason):
        for row in np.nonzero(rows)[0]:
            flagged.setdefault(int(labels[row]), []).append(reason)

    if depth >= 3 and n_planes is not None:
        flag(n_planes == 1, "flat")
    touches = column("touches_z_bounds")
    if touches is not None:
        flag(touches.astype(bool), "clipped_z")
    # Taller than wide, in µm: needs the table's µm columns (a table measured
    # in voxels cannot compare a plane with a pixel).
    z_step, xy_step = _step(frame, "Z", "z_um"), _step(frame, "X", "x_um")
    if z_step and xy_step and all(name in frame.columns for name in
                                  ("bbox_x0", "bbox_x1", "bbox_y0", "bbox_y1")):
        height = (z1 - z0 + 1) * z_step
        width = ((column("bbox_x1") - column("bbox_x0") + 1)
                 + (column("bbox_y1") - column("bbox_y0") + 1)) / 2.0 * xy_step
        flag(height > params["merged_ratio"] * width, "merged_z")
    volume = column("volume_um3", column("volume_vox"))
    if volume is not None and len(volume):
        volume = volume.astype(np.float64)
        logs = np.log(np.maximum(volume, 1e-9))
        mad = 1.4826 * float(np.median(np.abs(logs - np.median(logs)))) or 1e-9
        flag(np.abs(logs - np.median(logs)) / mad > params["volume_z"], "volume_outlier")
    if all(name in frame.columns for name in ("bbox_x0", "bbox_x1", "bbox_y0", "bbox_y1")):
        for a, b in _split_pairs(frame, params["split_iou"]):
            flagged.setdefault(int(labels[a]), []).append("split_z")
            flagged.setdefault(int(labels[b]), []).append("split_z")
    words = {"flat": "lie in one plane only (cut thin, or noise)",
             "clipped_z": "are cut off by the first or last plane",
             "merged_z": "are much taller than wide (cells stacked and merged)",
             "split_z": "are one cell split between neighbouring planes",
             "volume_outlier": "are far from the usual volume"}
    counts = {}
    for reasons in flagged.values():
        for reason in set(reasons):
            counts[reason] = counts.get(reason, 0) + 1
    findings = [{"check": "segmentation_3d", "channel": None, "kind": reason, "count": n,
                 "z0": 0, "z1": depth - 1, "severity": "warn",
                 "reason": f"{n} object{'' if n == 1 else 's'} {words[reason]}"}
                for reason, n in sorted(counts.items(), key=lambda item: -item[1])]
    return findings, flagged


def _step(frame, voxels, microns):
    """µm per plane (or pixel), from an object table's own voxel and µm
    centroid columns; None when it has no µm columns."""
    if voxels not in frame.columns or microns not in frame.columns:
        return None
    v, um = frame[voxels].to_numpy(), frame[microns].to_numpy()
    nonzero = np.abs(v) > 1e-9
    return float(np.median(um[nonzero] / v[nonzero])) if nonzero.any() else None


def _split_pairs(frame, threshold):
    """Row pairs (a, b): b starts the plane after a ends, and their XY boxes
    overlap by at least `threshold` (IoU). Candidates come from a grid of box
    centres a box's width across, so a table of many cells is not compared
    all against all."""
    x0, x1 = frame["bbox_x0"].to_numpy(), frame["bbox_x1"].to_numpy()
    y0, y1 = frame["bbox_y0"].to_numpy(), frame["bbox_y1"].to_numpy()
    z0, z1 = frame["bbox_z0"].to_numpy(), frame["bbox_z1"].to_numpy()
    if not len(x0):
        return []
    cell = max(1.0, float(np.median(np.maximum(x1 - x0, y1 - y0) + 1)))
    cx = ((x0 + x1) / 2.0 // cell).astype(np.int64)
    cy = ((y0 + y1) / 2.0 // cell).astype(np.int64)
    grid = {}
    for row in range(len(z0)):
        grid.setdefault((int(z0[row]), int(cx[row]), int(cy[row])), []).append(row)
    pairs = []
    for a in range(len(z1)):
        for gx in (cx[a] - 1, cx[a], cx[a] + 1):
            for gy in (cy[a] - 1, cy[a], cy[a] + 1):
                for b in grid.get((int(z1[a]) + 1, int(gx), int(gy)), ()):
                    ix = max(0, min(x1[a], x1[b]) - max(x0[a], x0[b]) + 1)
                    iy = max(0, min(y1[a], y1[b]) - max(y0[a], y0[b]) + 1)
                    inter = ix * iy
                    union = ((x1[a] - x0[a] + 1) * (y1[a] - y0[a] + 1)
                             + (x1[b] - x0[b] + 1) * (y1[b] - y0[b] + 1) - inter)
                    if union and inter / union >= threshold:
                        pairs.append((a, b))
    return pairs
