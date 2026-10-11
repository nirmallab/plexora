"""Linking a time series' objects from frame to frame: which cell is which.

A time series segmented frame by frame (a label volume per frame,
`labels.py`) numbers each frame's cells on its own: cell 12 in frame 3 and
cell 12 in frame 4 are strangers. This links them. Each pair of consecutive
frames is matched one to one on the distance between centroids -- in µm when
the object table has them (`x_um, y_um, z_um`), else in voxels with z scaled
by the stack's anisotropy -- by the assignment that minimises the summed
distance (the Hungarian method, `scipy.optimize.linear_sum_assignment`),
refusing any pair farther apart than `max_distance`. A cell matched carries
its track on; one not matched starts a track of its own. The result is a
`track` column in the object table: the same number in every frame for the
same cell.

What it does not do, and says so in its summary: divisions and merges (a
cell that splits continues as one of its daughters; the other starts a new
track), and gap closing (a cell missed in one frame starts a new track when
it is seen again). It is the linking a tracking package does first, not a
lineage.

A series whose ONE label volume is drawn on every frame needs no linking --
each id is the same cell in every frame -- and its tracks are its labels.

The distance cap, when not given, is the median distance from a cell to its
nearest neighbour in the first frame: a cell moving farther than that between
frames would be indistinguishable from its neighbour anyway, and linking it
would be a guess.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Above this many candidate pairs (cells in one frame x the next) a frame
#: pair is matched greedily, nearest first, instead of by the assignment.
DENSE_PAIRS = 16_000_000
#: What an impossible pair costs in the assignment: far above any real one.
_FAR = 1e12


@dataclass(frozen=True)
class Positions:
    """Where every row of an object table is, in the units distances use."""

    points: np.ndarray       # (N, 3)
    units: str               # "µm" or "voxels"


def positions(frame, *, anisotropy: float = 1.0) -> Positions:
    """Each row's centroid: µm columns when the table has them, else voxels
    with z stretched by `anisotropy` (z step / pixel size)."""
    columns = set(frame.columns)
    if {"x_um", "y_um", "z_um"} <= columns:
        points = np.column_stack([frame[name].to_numpy().astype(np.float64)
                                  for name in ("x_um", "y_um", "z_um")])
        return Positions(points, "µm")
    z = frame["Z"].to_numpy().astype(np.float64) * float(anisotropy or 1.0)
    points = np.column_stack([frame["X"].to_numpy().astype(np.float64),
                              frame["Y"].to_numpy().astype(np.float64), z])
    return Positions(points, "voxels")


def nearest_neighbour_median(points: np.ndarray) -> float | None:
    """The median distance from a point to its nearest other point."""
    if len(points) < 2:
        return None
    from scipy.spatial import cKDTree

    distances, _ = cKDTree(points).query(points, k=2)
    return float(np.median(distances[:, 1]))


def match(previous: np.ndarray, current: np.ndarray, max_distance: float):
    """Pairs `(i, j)` linking `previous[i]` to `current[j]`, one to one, none
    farther apart than `max_distance`; the assignment of least total distance
    (greedy, nearest first, for very large frames)."""
    if not len(previous) or not len(current):
        return []
    from scipy.spatial import cKDTree

    tree = cKDTree(current)
    near = tree.query_ball_point(previous, r=max_distance)
    candidates = [(i, j) for i, js in enumerate(near) for j in js]
    if not candidates:
        return []
    if len(previous) * len(current) <= DENSE_PAIRS:
        from scipy.optimize import linear_sum_assignment

        rows = sorted({i for i, _ in candidates})
        cols = sorted({j for _, j in candidates})
        row_of = {i: k for k, i in enumerate(rows)}
        col_of = {j: k for k, j in enumerate(cols)}
        cost = np.full((len(rows), len(cols)), _FAR)
        for i, j in candidates:
            cost[row_of[i], col_of[j]] = float(np.linalg.norm(previous[i] - current[j]))
        picked_rows, picked_cols = linear_sum_assignment(cost)
        return [(rows[a], cols[b]) for a, b in zip(picked_rows, picked_cols)
                if cost[a, b] < _FAR]
    distances = [(float(np.linalg.norm(previous[i] - current[j])), i, j) for i, j in candidates]
    distances.sort()
    used_i, used_j, pairs = set(), set(), []
    for _, i, j in distances:
        if i in used_i or j in used_j:
            continue
        used_i.add(i)
        used_j.add(j)
        pairs.append((i, j))
    return pairs


def link(frame, *, max_distance: float | None = None, anisotropy: float = 1.0,
         labels_per_frame: bool = True, progress=None):
    """`(tracks, summary)`: a track number for every row of `frame` (a polars
    object table with `t`, `label` and centroid columns), in row order.

    `labels_per_frame=False` (one label volume on every frame): the labels are
    the tracks. Otherwise consecutive frames are matched (`match`)."""
    t = frame["t"].to_numpy().astype(np.int64)
    labels = frame["label"].to_numpy().astype(np.int64)
    frames = sorted(int(v) for v in np.unique(t))
    summary = {"frames": len(frames), "objects": int(len(labels))}
    if not labels_per_frame:
        summary.update(method="labels", tracks=int(len(np.unique(labels))),
                       note="one mask is drawn on every frame: each label is the same cell "
                            "in every frame")
        return labels.copy(), summary
    where = positions(frame, anisotropy=anisotropy)
    first = where.points[t == frames[0]] if frames else where.points[:0]
    if max_distance is None:
        max_distance = nearest_neighbour_median(first)
        chosen = "median nearest-neighbour distance in the first frame"
        if max_distance is None:
            max_distance, chosen = 0.0, "no neighbours to measure"
    else:
        chosen = "given"
    tracks = np.full(len(labels), -1, dtype=np.int64)
    next_track = 1
    linked = 0
    previous_rows = None
    for index, value in enumerate(frames):
        rows = np.flatnonzero(t == value)
        if previous_rows is not None and max_distance > 0:
            pairs = match(where.points[previous_rows], where.points[rows], max_distance)
            for i, j in pairs:
                tracks[rows[j]] = tracks[previous_rows[i]]
            linked += len(pairs)
        for row in rows:
            if tracks[row] < 0:
                tracks[row] = next_track
                next_track += 1
        previous_rows = rows
        if progress is not None:
            progress(index + 1, len(frames))
    lengths = np.bincount(tracks)[1:]
    lengths = lengths[lengths > 0]
    possible = int(len(labels) - (np.sum(t == frames[0]) if frames else 0))
    summary.update(
        method="assignment" if len(frames) < 2 or all(
            np.sum(t == a) * np.sum(t == b) <= DENSE_PAIRS for a, b in zip(frames, frames[1:]))
        else "greedy",
        tracks=int(len(lengths)),
        linked=int(linked),
        linked_fraction=(linked / possible) if possible else None,
        mean_length=float(lengths.mean()) if len(lengths) else 0.0,
        full_length=int(np.sum(lengths == len(frames))),
        max_distance=float(max_distance), max_distance_from=chosen, units=where.units,
        not_done=["divisions and merges", "gap closing (a cell missed for a frame starts "
                  "a new track)"])
    return tracks, summary


def track_project_objects(project, *, max_distance=None, progress=None) -> dict:
    """Link the project's measured objects (`objects_v1.parquet`) across
    frames: writes a `track` column into the table and a `tracking` entry
    into its manifest. Returns the summary."""
    import json

    import polars as pl

    from plexora.server.volume import objects as volume_objects

    table_path, manifest_path = volume_objects.object_paths(project)
    if not table_path.exists():
        raise volume_objects.ObjectsUnavailable(
            "measure this project's cells first: there is no object table to link")
    frame = pl.read_parquet(table_path)
    if "t" not in frame.columns or frame["t"].n_unique() < 2:
        raise volume_objects.ObjectsUnavailable(
            "the object table has one frame: there is nothing to link across")
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    anisotropy = 1.0
    spacing = manifest.get("spacing_um") or {}
    if spacing.get("z") and spacing.get("x"):
        anisotropy = float(spacing["z"]) / float(spacing["x"])
    tracks, summary = link(frame, max_distance=max_distance, anisotropy=anisotropy,
                           labels_per_frame=bool(manifest.get("labels_per_frame", True)),
                           progress=progress)
    frame = frame.with_columns(pl.Series("track", tracks))
    tmp = table_path.with_name(table_path.name + ".tmp")
    frame.write_parquet(tmp)
    tmp.replace(table_path)
    manifest["tracking"] = summary
    manifest["columns"] = list(frame.columns)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    volume_objects.note_table_column(project, table_path, "track")
    return summary
