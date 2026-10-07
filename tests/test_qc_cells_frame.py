"""The per-cell frame `calls.derive` builds, column by column from flat arrays
(`calls._cells_frame`), is exactly the frame the row-by-row Python lists made.

`_reference_frame` is that earlier construction, kept verbatim as the
reference: on random masks and on a whole `derive` over the synthetic project
(regions of both levels, segmentation flags, a dismissal), the two must agree
to the last list element and the summary must not move.
"""

import numpy as np
import polars as pl
import pytest

from plexora.agent import AgentSession
from plexora.plugins.qc.server import schemas, strictness
from plexora.plugins.qc.server.cells import calls
from tests.qc_fixtures import make_qc_project


def _reference_frame(ids, result_id, exclude, warn, note, flags, roi_ids, roi_method,
                     background):
    """The construction `_cells_frame` replaced (lists of lists per row)."""
    n = int(ids.size)
    rows_roi = [list(roi_ids.get(i, [])) for i in range(n)]
    failing = np.zeros(n, dtype=bool)
    warned = np.zeros(n, dtype=bool)
    for reason in schemas.REASONS:
        failing |= exclude[reason]
        warned |= warn[reason]
    reasons_list = [[] for _ in range(n)]
    excluded_by = [[] for _ in range(n)]
    noted_by = [[] for _ in range(n)]
    primary = np.full(n, "", dtype=object)
    order = {r: i for i, r in enumerate(schemas.PRIMARY_ORDER)}
    for reason in sorted(schemas.REASONS, key=lambda r: order.get(r, 999)):
        hits = np.flatnonzero(exclude[reason] | warn[reason] | note[reason])
        for row in hits:
            reasons_list[row].append(reason)
        for row in np.flatnonzero(note[reason] & ~exclude[reason] & ~warn[reason]):
            noted_by[row].append(reason)
        for row in np.flatnonzero(exclude[reason]):
            excluded_by[row].append(reason)
            if not primary[row]:
                primary[row] = reason
    marker_flags = [[] for _ in range(n)]
    unreliable = [[] for _ in range(n)]
    by_marker = {}
    for (marker, reason), masks in sorted(flags.items()):
        for status in ("exclude", "warn"):
            rows = np.flatnonzero(masks[status])
            if not rows.size:
                continue
            by_marker.setdefault(marker, {}).setdefault(reason, {})[status] = int(rows.size)
            for row in rows:
                marker_flags[row].append(f"{marker}|{reason}|{status}")
                if status == "exclude" and marker not in unreliable[row]:
                    unreliable[row].append(marker)
    action = np.where(failing, "exclude", np.where(warned, "warn", "pass"))
    frame = pl.DataFrame({
        "cell_id": pl.Series(ids, dtype=pl.Int64),
        "pass": pl.Series(~failing, dtype=pl.Boolean),
        "action": pl.Series(action.tolist(), dtype=pl.Utf8),
        "primary_reason": pl.Series(primary.tolist(), dtype=pl.Utf8),
        "reasons": pl.Series(reasons_list, dtype=pl.List(pl.Utf8)),
        "reason_count": pl.Series([len(r) for r in reasons_list], dtype=pl.Int16),
        "excluded_by": pl.Series(excluded_by, dtype=pl.List(pl.Utf8)),
        "noted_by": pl.Series(noted_by, dtype=pl.List(pl.Utf8)),
        "background": pl.Series(background, dtype=pl.Boolean),
        "unreliable_markers": pl.Series([sorted(u) for u in unreliable],
                                        dtype=pl.List(pl.Utf8)),
        "marker_flags": pl.Series(marker_flags, dtype=pl.List(pl.Utf8)),
        "roi_ids": pl.Series(rows_roi, dtype=pl.List(pl.Utf8)),
        "roi_method": pl.Series(roi_method.tolist(), dtype=pl.Utf8),
        "result_id": pl.Series([result_id] * n, dtype=pl.Utf8),
    })
    noted_any = np.array([bool(v) for v in noted_by], dtype=bool)
    flagged_any = np.array([bool(f) for f in marker_flags], dtype=bool)
    unreliable_any = np.array([bool(u) for u in unreliable], dtype=bool)
    return frame, failing, warned, by_marker, noted_any, flagged_any, unreliable_any


def _same(a, b):
    frame_a, *rest_a = a
    frame_b, *rest_b = b
    assert frame_a.schema == frame_b.schema
    for column in frame_a.columns:
        assert frame_a[column].to_list() == frame_b[column].to_list(), column
    assert frame_a.equals(frame_b)
    for x, y in zip(rest_a, rest_b):
        if isinstance(x, np.ndarray):
            assert np.array_equal(x, y)
        else:
            assert x == y


def _random_inputs(n, seed):
    rng = np.random.default_rng(seed)
    ids = rng.permutation(np.arange(1, n + 1) * 7).astype(np.int64)

    def masks(p):
        return {r: rng.random(n) < p for r in schemas.REASONS}

    exclude, warn, note = masks(0.03), masks(0.04), masks(0.05)
    markers = ["CD3", "CD20", "SMA", "FOXP3", "DNA_2", "Ki67"]
    flags = {}
    for marker in markers:
        for reason in rng.choice(list(schemas.MARKER_REASONS), size=3, replace=False):
            flags[(marker, str(reason))] = {"exclude": rng.random(n) < 0.04,
                                            "warn": rng.random(n) < 0.06}
    flags[("CD3", "dna_loss")] = {"exclude": np.zeros(n, dtype=bool),
                                  "warn": np.zeros(n, dtype=bool)}  # flags nobody
    # Regions listed in an order that differs from row to row.
    roi_ids = {}
    for row in rng.choice(n, size=n // 5, replace=False).tolist() if n else []:
        k = int(rng.integers(1, 4))
        roi_ids[row] = [f"roi_{int(i)}" for i in rng.choice(9, size=k, replace=False)]
    roi_method = np.full(n, None, dtype=object)
    for row in roi_ids:
        roi_method[row] = str(rng.choice(["mask", "centroid", "mixed"]))
    background = exclude["background"] | warn["background"] | note["background"]
    return ids, "qr_x", exclude, warn, note, flags, roi_ids, roi_method, background


@pytest.mark.parametrize("n, seed", [(0, 0), (1, 1), (37, 2), (5000, 3)])
def test_the_flat_construction_is_the_list_construction(n, seed):
    inputs = _random_inputs(n, seed)
    _same(calls._cells_frame(*inputs), _reference_frame(*inputs))


def test_derive_is_unchanged_on_the_synthetic_project(tmp_path, monkeypatch):
    make_qc_project(tmp_path, artifacts=())
    ds = AgentSession().data("qcsynth")
    ids = calls._rows(ds)[0]
    markers = [m for m in ds.table.markers if not m.upper().startswith("DNA")]
    one, two = markers[0], markers[1]
    result = {"result_id": "qr_same", "cycles": [], "candidates": {
        "r_fold": {"id": "r_fold", "roi_id": "r_fold", "class": "tissue_fold",
                   "scope": "all_channels", "channels": ["DNA_1"], "action": "exclude"},
        "r_edge": {"id": "r_edge", "roi_id": "r_edge", "class": "slide_or_tissue_edge",
                   "scope": "all_channels", "channels": [], "action": "warn"},
        "r_focus": {"id": "r_focus", "roi_id": "r_focus", "class": "out_of_focus",
                    "scope": "channel", "channels": [one, two], "action": "warn"},
        "r_bubble": {"id": "r_bubble", "roi_id": "r_bubble", "class": "air_bubble_or_coverslip",
                     "scope": "channel", "channels": [one], "action": "exclude"},
        "r_agg": {"id": "r_agg", "roi_id": "r_agg", "class": "antibody_aggregate",
                  "scope": "channel", "channels": [two], "action": "exclude"},
        "c_reg": {"id": "c_reg", "class": "cross_cycle_registration_error",
                  "channel_level": True, "channels": [two], "action": "warn"}},
        "user_dismissed": [{"finding": "cell_reason", "reason": "seg_over"}]}
    # Overlapping regions, entered in different orders for different cells.
    spans = [("r_edge", 30, 70), ("r_fold", 0, 60), ("r_focus", 40, 110),
             ("r_bubble", 20, 50), ("r_agg", 80, 120), ("r_edge", 100, 115)]
    cell_ids, roi_ids, fractions, methods = [], [], [], []
    for k, (roi, lo, hi) in enumerate(spans):
        cell_ids += ids[lo:hi].tolist()
        roi_ids += [roi] * (hi - lo)
        fractions += np.linspace(0.2, 1.0, hi - lo).tolist()
        methods += ["mask" if k % 2 else "centroid"] * (hi - lo)
    pairs = pl.DataFrame({"cell_id": pl.Series(cell_ids, dtype=pl.Int64),
                          "roi_id": pl.Series(roi_ids, dtype=pl.Utf8),
                          "fraction": pl.Series(fractions, dtype=pl.Float32),
                          "method": pl.Series(methods, dtype=pl.Utf8)})
    pairs = pairs.unique(subset=["cell_id", "roi_id"], keep="first", maintain_order=True)
    columns = {c: pl.Series(np.zeros(ids.size, dtype=bool)) for c in calls.SEG_COLUMNS.values()}
    for column, rows in (("under_segmented", slice(0, 5)), ("over_segmented", slice(5, 9)),
                         ("large", slice(100, 104)), ("irregular", slice(50, 55))):
        values = columns[column].to_numpy().copy()
        values[rows] = True
        columns[column] = pl.Series(values)
    seg = ({"version": "v", "fingerprint": "fp", "dna_channel": "DNA_1"},
           pl.DataFrame({"cell_id": pl.Series(ids, dtype=pl.Int64), **columns}),
           {"under": 0.6, "over": 0.6, "large": 3.0, "small": 3.0, "irregular": 3.0})
    for preset in ("standard", "strict"):
        table = strictness.thresholds(preset)
        new = calls.derive(ds, result, table, pairs=pairs, seg=seg)
        with monkeypatch.context() as patch:
            patch.setattr(calls, "_cells_frame", _reference_frame)
            old = calls.derive(ds, result, table, pairs=pairs, seg=seg)
        assert new[0].equals(old[0]), preset
        assert new[1].equals(old[1])
        assert new[2] == old[2]
        # Every part was exercised: cells in several regions, marker flags,
        # unreliable markers, notes and exclusions.
        assert any(len(r) >= 3 for r in new[0]["roi_ids"].to_list())
        assert new[0]["marker_flags"].list.len().sum() > 0
        assert new[0]["unreliable_markers"].list.len().sum() > 0
        assert new[0]["noted_by"].list.len().sum() > 0
        assert new[0]["excluded_by"].list.len().sum() > 0


def test_propagation_counts_the_same_without_the_sort(tmp_path, monkeypatch):
    """`_overlap`'s dense count (one bincount over the label range) gives the
    pairs the per-block `np.unique` gave -- which it still falls back to."""
    from plexora.plugins.qc.server import propagate

    make_qc_project(tmp_path, artifacts=())
    ds = AgentSession().data("qcsynth")

    def box(x0, y0, x1, y1):
        return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                    [x0, y0]]]}

    ring = {"type": "Polygon", "coordinates": [
        [[100, 100], [700, 100], [700, 700], [100, 700], [100, 100]],
        [[300, 300], [500, 300], [500, 500], [300, 500], [300, 300]]]}
    regions = [{"roi_id": "a", "geometry": box(0, 0, 300, 260)},
               {"roi_id": "b", "geometry": ring},
               {"roi_id": "c", "geometry": box(650, 600, 1024, 1024)}]
    dense = propagate.propagate(ds, regions, median_diameter_px=12.0)
    # A tiny block forces several blocks per region, so the sums across blocks count too.
    monkeypatch.setattr(propagate, "BLOCK_PX", 128)
    blocks = propagate.propagate(ds, regions, median_diameter_px=12.0)
    monkeypatch.setattr(propagate, "DENSE_LABEL_MAX", -1)
    sorted_ = propagate.propagate(ds, regions, median_diameter_px=12.0)
    assert dense[0].height and set(dense[0]["method"].to_list()) == {"mask"}
    for other in (blocks, sorted_):
        assert dense[0].equals(other[0])
        assert dense[1] == other[1]
