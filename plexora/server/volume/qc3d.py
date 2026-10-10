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
    # Duplicate: a plane whose mean absolute difference from the previous one
    # is below this many grey levels. A plane written twice is bit-identical;
    # correlation would also flag neighbouring planes of a sharp, quiet stack.
    "duplicate_mad": 0.5,
    # Outlier: a plane's median differs from its neighbours' by more than this
    # many robust standard deviations of the plane-to-plane differences.
    "outlier_z": 4.0,
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


def focus_profile(stats, channel=None, params=PARAMS):
    focus = np.array([row["focus"] for row in stats], dtype=np.float64)
    if not len(focus) or focus.max() <= 0:
        return []
    share = focus / focus.max()
    flags = share < params["focus_deficit"]
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


def missing_slices(planes, stats, channel=None, params=PARAMS):
    findings = []
    for row in stats:
        if row["max"] <= 0:
            findings.append({"check": "missing_slices", "channel": channel, "z0": row["z"],
                             "z1": row["z"], "severity": "fail",
                             "reason": f"plane {row['z']} is empty (every pixel zero)",
                             "kind": "empty"})
    for z in range(1, len(planes)):
        a = np.asarray(planes[z - 1], dtype=np.float64)
        b = np.asarray(planes[z], dtype=np.float64)
        if not a.size or a.max() <= 0:
            continue
        mad = float(np.mean(np.abs(a - b)))
        if mad < params["duplicate_mad"]:
            findings.append({"check": "missing_slices", "channel": channel, "z0": z, "z1": z,
                             "severity": "fail", "kind": "duplicate",
                             "reason": f"plane {z} repeats plane {z - 1} (mean difference "
                                       f"{mad:.2f} grey levels)"})
    medians = np.array([row["median"] for row in stats], dtype=np.float64)
    if len(medians) >= 5:
        neighbours = np.array([np.median(np.concatenate([medians[max(0, z - 2):z],
                                                         medians[z + 1:z + 3]]))
                               for z in range(len(medians))])
        diff = medians - neighbours
        scale = 1.4826 * np.median(np.abs(np.diff(medians))) or 1e-9
        for z in np.nonzero(np.abs(diff) > params["outlier_z"] * scale)[0]:
            if any(f["z0"] == int(z) for f in findings):
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
