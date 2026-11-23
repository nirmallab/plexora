"""Stage 1 of the pixel-evidence study: offline, no engine change, no model cost.

    PLEXORA_TESTING=1 PLEXORA_DATA_PATH=<scratch> PYTHONPATH=. \\
        python tools/bench_gates/pixel_evidence.py <command> ...

Commands (docs/internal/bench/PIXEL_EVIDENCE.md has the plan and the rules):

  e1 <labels dir> [...] --out e1.json
      Per cell, on the person-labelled near-gate cells of each label set
      (`runs/2026-10-09-labels{,-crc,-imc,-codex-ln}`): how well the
      neighbour-referenced contrast separates own stain from not-own, by
      platform, crowding and cell size; the table version (segmentation
      means) against the pixel version (centroid disks). Read from the sets'
      own crops: nothing else is needed.

  prepare --image <name> --out <dir>
      Per cell and marker for one image: the table value (log1p), the table
      contrast, and with --pixel the disk means at a pyramid level and the
      pixel contrast. Images: `lsp11385_2` and `lsp35766_p193_2` (the two
      held on this machine; see IMAGES).

  e2 --prepared <dir> [...] --truth <json> --sessions <dir> --out e2.json
      Gates: the pixel gate (and the comparator, and signal B) against the
      person's gates per cohort, paired with the scored start, Otsu, the
      mixture and the engine's stored sessions on the same markers.

  e3 --prepared <dir> [...] --truth <json> --sessions <dir> --out e3.json
      Frozen-reads replay: where a stored session closed unresolved, the
      plateau member the pixel loss favours instead of the gate written.
      E3b (the checks) is scored in the same pass.

Everything is computed from stored data; nothing is written to a project.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2]))
sys.path.insert(0, str(HERE.parent))

import pixel_signals as pe  # noqa: E402

TRUTH_LABEL = {"own": 1, "glow": 0, "none": 0}


# -- E1: per cell, on the label sets --------------------------------------------------------


def _auc(score, y):
    score = np.asarray(score, dtype=np.float64)
    y = np.asarray(y, dtype=np.int64)
    ok = np.isfinite(score)
    score, y = score[ok], y[ok]
    pos, neg = score[y == 1], score[y == 0]
    if not pos.size or not neg.size:
        return None
    from scipy.stats import mannwhitneyu

    u = mannwhitneyu(pos, neg, alternative="two-sided").statistic
    return float(u / (pos.size * neg.size))


def _balanced(pred, y):
    pred = np.asarray(pred, dtype=bool)
    y = np.asarray(y, dtype=np.int64)
    if not (y == 1).any() or not (y == 0).any():
        return None
    return float(0.5 * (pred[y == 1].mean() + (~pred[y == 0]).mean()))


def _centroids(labels):
    from scipy import ndimage

    ids = np.unique(labels)
    ids = ids[ids > 0]
    if not ids.size:
        return ids, np.zeros((0, 2))
    c = ndimage.center_of_mass(np.ones_like(labels), labels, ids)
    return ids, np.asarray(c, dtype=np.float64)


def _crop_measures(marker, labels, diameter):
    """Own and brightest-neighbour values in one crop: mask means (the
    table version) and centroid-disk means (the pixel version)."""
    from scipy import ndimage

    ids, cents = _centroids(labels)
    if 1 not in ids:
        return None
    plane = marker.astype(np.float64)
    means = ndimage.mean(plane, labels, ids)
    own_i = int(np.flatnonzero(ids == 1)[0])
    d = np.hypot(*(cents - cents[own_i]).T)
    nb = [i for i in range(len(ids)) if i != own_i and d[i] <= pe.REACH * diameter]
    r = pe.DISK_FRACTION * diameter
    dy, dx = pe.disk_offsets(r)
    h, w = plane.shape

    def disk(cy, cx):
        yy = np.rint(cy).astype(int) + dy
        xx = np.rint(cx).astype(int) + dx
        ok = (yy >= 0) & (yy < h) & (xx >= 0) & (xx < w)
        return float(plane[yy[ok], xx[ok]].mean()) if ok.any() else np.nan

    own_disk = disk(*cents[own_i])
    out = {"own_mask": float(means[own_i]), "own_disk": own_disk, "n_nb": len(nb),
           "area": int((labels == 1).sum())}
    if nb:
        out["nb_mask"] = float(max(means[i] for i in nb))
        out["nb_disk"] = float(np.nanmax([disk(*cents[i]) for i in nb]))
    else:
        out["nb_mask"] = out["nb_disk"] = np.nan
    return out


def e1_set(folder: Path) -> dict:
    key = json.loads((folder / "key.json").read_text())
    tiles = key["tiles"]
    labels = json.loads((folder / "labels.json").read_text())["labels"]
    z = np.load(folder / "crops.npz")
    own_areas = [t["measure"]["area_px"] for t in tiles.values()
                 if isinstance(t.get("measure"), dict) and t["measure"].get("area_px")]
    diameter = pe.cell_diameter(own_areas)
    rows = []
    for tid, t in tiles.items():
        lab = labels.get(tid)
        if not lab or lab["label"] not in TRUTH_LABEL or f"{tid}_labels" not in z.files:
            continue
        m = _crop_measures(z[f"{tid}_marker"], z[f"{tid}_labels"].astype(np.int64), diameter)
        if m is None:
            continue
        rows.append({"tile": tid, "marker": t["marker"], "y": TRUTH_LABEL[lab["label"]],
                     "label": lab["label"], "seg": bool(lab.get("seg")),
                     "dense": bool(t.get("dense")), "group": t.get("group"),
                     "z": (t["fit_value"] - t["fit_gate"]) / (t.get("band_half_fit") or 1.0),
                     "c_table": math.log1p(m["own_mask"]) - math.log1p(m["nb_mask"])
                     if np.isfinite(m["nb_mask"]) else np.nan,
                     "c_pixel": math.log1p(m["own_disk"]) - math.log1p(m["nb_disk"])
                     if np.isfinite(m["nb_disk"]) and np.isfinite(m["own_disk"]) else np.nan,
                     "n_nb": m["n_nb"], "area": m["area"]})
    return {"set": key.get("set_id"), "project": key.get("project"),
            "gates_source": key.get("gates_source") or key.get("gates_column"),
            "diameter_px": diameter, "rows": rows}


def e1_summary(rows) -> dict:
    """AUC of each score for own vs not-own, the balanced accuracy of the
    structural rule contrast > 0 (a local peak), and the share unscored (no
    neighbour within reach)."""
    out = {}
    if not rows:
        return out
    y = np.array([r["y"] for r in rows])
    out["n"] = len(rows)
    out["own"] = int(y.sum())
    for name in ("z", "c_table", "c_pixel"):
        s = np.array([r[name] for r in rows], dtype=np.float64)
        out[f"auc_{name}"] = _auc(s, y)
        if name != "z":
            scored = np.isfinite(s)
            out[f"unscored_{name}"] = round(float((~scored).mean()), 3)
            out[f"bacc_{name}_gt0"] = _balanced(s[scored] > 0, y[scored]) if scored.any() else None
    # Per marker AUCs, then their mean (each marker's own ranking).
    per = defaultdict(list)
    for r in rows:
        per[r["marker"]].append(r)
    for name in ("z", "c_table", "c_pixel"):
        vals = [_auc([r[name] for r in rs], [r["y"] for r in rs]) for rs in per.values()]
        vals = [v for v in vals if v is not None]
        out[f"marker_auc_{name}"] = round(float(np.mean(vals)), 3) if vals else None
    return out


def cmd_e1(args):
    report = {}
    for folder in args.sets:
        res = e1_set(Path(folder))
        rows = res["rows"]
        areas = np.array([r["area"] for r in rows], dtype=np.float64)
        t1, t2 = (np.quantile(areas, [1 / 3, 2 / 3]) if rows else (0, 0))
        strata = {
            "all": rows,
            "dense": [r for r in rows if r["dense"]],
            "not_dense": [r for r in rows if not r["dense"]],
            "few_nb(<=2)": [r for r in rows if r["n_nb"] <= 2],
            "many_nb(>=5)": [r for r in rows if r["n_nb"] >= 5],
            "small_cells": [r for r in rows if r["area"] <= t1],
            "large_cells": [r for r in rows if r["area"] > t2],
            "seg_flagged": [r for r in rows if r["seg"]],
        }
        report[res["set"]] = {"project": res["project"], "gates_source": res["gates_source"],
                              "diameter_px": res["diameter_px"],
                              "strata": {k: e1_summary(v) for k, v in strata.items()},
                              "markers": {m: e1_summary([r for r in rows if r["marker"] == m])
                                          for m in sorted({r["marker"] for r in rows})}}
        if args.rows:
            report[res["set"]]["rows"] = rows
    Path(args.out).write_text(json.dumps(report, indent=1, default=float))
    for name, rep in report.items():
        a = rep["strata"]["all"]
        print(f"{name:34} n={a.get('n')} own={a.get('own')}  AUC z {a.get('auc_z'):.2f}  "
              f"table {a.get('auc_c_table') or float('nan'):.2f}  pixel {a.get('auc_c_pixel') or float('nan'):.2f}  "
              f"| per-marker table {a.get('marker_auc_c_table')} pixel {a.get('marker_auc_c_pixel')}  "
              f"| bacc>0 table {a.get('bacc_c_table_gt0')} pixel {a.get('bacc_c_pixel_gt0')}")


# -- the two images held here ---------------------------------------------------------------

IMAGES = {
    "lsp11385_2": {
        "cohort": "PCA (LSP CyCIF, melanoma panel)",
        "table": "/Users/aj/Downloads/LSP11385_auto/LSP11385--mccellpose_cell.csv",
        "image": "/Users/aj/Downloads/LSP11385_auto/LSP11385.ome.tif",
        "channels": "/Users/aj/Downloads/LSP11385_auto/markers.csv",
        "pixel_um": 0.325,
    },
    "lsp35766_p193_2": {
        "cohort": "CRC (LSP CyCIF, colorectal panel)",
        "table": "/Users/aj/Downloads/crc/b015_postxenium_cycif.xenium_default.combined.h5ad",
        "section": "s04_cycif_LSP35766A",
        "image": "/Users/aj/Downloads/crc/LSP35766_P193.ome.tif",
        "channels": "/Users/aj/Downloads/crc/markers.csv",
        "pixel_um": 0.325,
    },
}

#: Non-marker channels: DNA and blanks are never gated.
SKIP = ("DNA", "Nucleus", "AF1", "AF2", "AF3", "Hoechst", "DAPI")


def _channels(path):
    """Channel names in image order: a markers.csv with `marker_name`, or one
    name per line."""
    lines = [ln.strip() for ln in Path(path).read_text().splitlines() if ln.strip()]
    head = [h.strip() for h in lines[0].split(",")]
    if "marker_name" in head:
        k = head.index("marker_name")
        return [ln.split(",")[k].strip() for ln in lines[1:]]
    return lines


def _load_table(name):
    spec = IMAGES[name]
    if spec["table"].endswith(".csv"):
        import pandas as pd

        head = pd.read_csv(spec["table"], nrows=0).columns
        chan = _channels(spec["channels"])
        markers = [c for c in chan if c in head and not c.startswith(SKIP)]
        df = pd.read_csv(spec["table"], usecols=["CellID", "X_centroid", "Y_centroid", "Area",
                                                  *markers])
        values = np.log1p(np.clip(df[markers].to_numpy(np.float64), 0, None))
        xy = df[["X_centroid", "Y_centroid"]].to_numpy(np.float64)
        area_px = df["Area"].to_numpy(np.float64)
        ids = df["CellID"].to_numpy()
        return {"markers": markers, "values": values, "xy": xy, "area_px": area_px,
                "ids": ids, "channels": chan}
    import anndata as ad

    a = ad.read_h5ad(spec["table"], backed="r")
    sec = np.flatnonzero(a.obs["section_id"].to_numpy() == spec["section"])
    lo, hi = int(sec.min()), int(sec.max()) + 1
    assert hi - lo == sec.size, "section rows are not contiguous"
    sub = a[lo:hi]
    X = np.asarray(sub.X[:] if not hasattr(sub.X, "toarray") else sub.X.toarray(), dtype=np.float64)
    var = [str(v) for v in a.var_names]
    obs = sub.obs
    xy = np.c_[obs["reg_cycif_centroid_x_whole_px"].to_numpy(np.float64),
               obs["reg_cycif_centroid_y_whole_px"].to_numpy(np.float64)]
    area_px = obs["area_um2"].to_numpy(np.float64) / spec["pixel_um"] ** 2
    measured = (X >= 0).any(axis=1)
    X = np.where(X < 0, np.nan, X)
    chan = _channels(spec["channels"])
    markers = [m for m in var if not m.startswith(SKIP)]
    keep = [var.index(m) for m in markers]
    values = np.log1p(np.clip(X[:, keep], 0, None))
    values[~measured] = np.nan
    return {"markers": markers, "values": values, "xy": xy, "area_px": area_px,
            "ids": np.asarray(obs.index), "channels": chan}


def _channel_index(channels, marker):
    try:
        return channels.index(marker)
    except ValueError:
        return None


def cmd_prepare(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    tab = _load_table(args.image)
    D = pe.cell_diameter(tab["area_px"])
    xy = tab["xy"]
    idx, count = pe.neighbours(xy, D)
    print(f"{args.image}: {xy.shape[0]} cells, {len(tab['markers'])} markers, D={D:.1f} px, "
          f"median neighbours {np.median(count):.0f}, none {np.mean(count == 0):.3f} "
          f"({time.time() - t0:.0f}s)", flush=True)
    np.savez_compressed(out / "geometry.npz", xy=xy, area_px=tab["area_px"], idx=idx,
                        count=count, diameter=D)
    meta = {"image": args.image, "cohort": IMAGES[args.image]["cohort"], "diameter_px": D,
            "markers": tab["markers"], "n_cells": int(xy.shape[0]), "pixel": None}
    np.save(out / "values.npy", tab["values"].astype(np.float32))
    c_table = np.stack([pe.contrast(tab["values"][:, j], idx)
                        for j in range(len(tab["markers"]))], axis=1)
    nb_mean = np.stack([pe.neighbour_mean(tab["values"][:, j], idx)
                        for j in range(len(tab["markers"]))], axis=1)
    np.save(out / "contrast_table.npy", c_table.astype(np.float32))
    np.save(out / "nb_mean_table.npy", nb_mean.astype(np.float32))
    print(f"table contrast done ({time.time() - t0:.0f}s)", flush=True)
    if args.pixel:
        import tifffile
        import zarr

        level = int(args.level)
        store = tifffile.imread(IMAGES[args.image]["image"], aszarr=True, level=level)
        arr = zarr.open(store, mode="r")
        scale = 2 ** level
        D_px = D / scale
        if D_px < pe.MIN_DIAMETER_PX:
            raise SystemExit(f"cell diameter {D_px:.1f} px at level {level} < {pe.MIN_DIAMETER_PX}")
        r = pe.DISK_FRACTION * D_px
        xs, ys = xy[:, 0] / scale, xy[:, 1] / scale
        ok = np.isfinite(xs) & np.isfinite(ys)
        m = int(math.ceil(r)) + 2
        x0 = max(int(np.floor(np.nanmin(xs[ok]))) - m, 0)
        x1 = min(int(np.ceil(np.nanmax(xs[ok]))) + m + 1, arr.shape[-1])
        y0 = max(int(np.floor(np.nanmin(ys[ok]))) - m, 0)
        y1 = min(int(np.ceil(np.nanmax(ys[ok]))) + m + 1, arr.shape[-2])
        disks = np.full((xy.shape[0], len(tab["markers"])), np.nan, dtype=np.float32)
        only = set(args.markers.split(",")) if args.markers else None
        for j, mk in enumerate(tab["markers"]):
            if only and mk not in only:
                continue
            c = _channel_index(tab["channels"], mk)
            if c is None:
                print(f"  {mk}: no channel", flush=True)
                continue
            t1 = time.time()
            plane = np.asarray(arr[c, y0:y1, x0:x1])
            dm = np.full(xy.shape[0], np.nan)
            dm[ok] = pe.disk_means(plane, ys[ok] - y0, xs[ok] - x0, r)
            disks[:, j] = dm
            del plane
            print(f"  {mk}: disk means in {time.time() - t1:.0f}s", flush=True)
            np.save(out / "disk_means.npy", disks)
        c_pix = np.stack([pe.contrast(np.log1p(disks[:, j].astype(np.float64)), idx)
                          for j in range(len(tab["markers"]))], axis=1)
        np.save(out / "contrast_pixel.npy", c_pix.astype(np.float32))
        meta["pixel"] = {"level": level, "radius_px": r, "diameter_px_at_level": D_px}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"done in {time.time() - t0:.0f}s", flush=True)


# -- E2 / E3: gates against the person's -----------------------------------------------------

LEVELS = (0.5, 0.25, 0.1)


class Prepared:
    """One `prepare` folder: values, contrasts and geometry of one image."""

    def __init__(self, folder):
        folder = Path(folder)
        self.folder = folder
        self.meta = json.loads((folder / "meta.json").read_text())
        self.image = self.meta["image"]
        self.markers = list(self.meta["markers"])
        self.values = np.load(folder / "values.npy", mmap_mode="r")
        self.c_table = np.load(folder / "contrast_table.npy", mmap_mode="r")
        self.nb_mean = np.load(folder / "nb_mean_table.npy", mmap_mode="r")
        g = np.load(folder / "geometry.npz")
        self.xy, self.count = g["xy"], g["count"]
        px = folder.parent / (folder.name + "_px")
        self.c_pixel = None
        if (px / "contrast_pixel.npy").exists():
            meta = json.loads((px / "meta.json").read_text())
            if meta["markers"] == self.markers:
                self.c_pixel = np.load(px / "contrast_pixel.npy", mmap_mode="r")

    def column(self, marker):
        j = self.markers.index(marker)
        cp = None
        if self.c_pixel is not None:
            cp = np.asarray(self.c_pixel[:, j], dtype=np.float64)
            if not np.isfinite(cp).any():
                cp = None
        return (np.asarray(self.values[:, j], dtype=np.float64),
                np.asarray(self.c_table[:, j], dtype=np.float64), cp,
                np.asarray(self.nb_mean[:, j], dtype=np.float64))


def _background(values):
    from plexora.plugins.gating.server.autogate.scoring import background

    body = values[np.isfinite(values)]
    bg = background(body)
    return None if bg is None else {k: bg[k] for k in ("mode", "sd", "onset", "ceiling")}


def load_sessions(folder, project):
    """[(session id, created, {marker: unit})] of every stored session with
    units of `project`, oldest first."""
    out = []
    for d in sorted(Path(folder).iterdir()):
        f = d / "session.json"
        if not f.exists():
            continue
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        units = {u.get("marker"): u for u in (s.get("units") or {}).values()
                 if isinstance(u, dict) and u.get("project") == project}
        if units:
            out.append((d.name, s.get("created_at") or d.name, units))
    return out


def _lattice_point(unit, family):
    for pt in ((unit.get("lattice") or {}).get("points") or ()):
        if pt.get("id") == family:
            return float(pt["low"])
    return None


def _is_gate(truth, values):
    """A person's gate that is a gate: below the column's top (a gate at
    the top says "no positives", which no estimator here is asked)."""
    finite = values[np.isfinite(values)]
    return truth is not None and finite.size and truth < float(np.quantile(finite, 0.999))


def marker_evidence(prep, marker, sessions):
    """Everything E2 needs for one marker of one image."""
    v, ct, cp, nbm = prep.column(marker)
    bg = _background(v)
    if bg is None:
        return None
    bounds = pe.grid(v, bg["mode"])
    out = {"marker": marker, "background": bg, "grid": [bounds[0], bounds[-1], len(bounds)]
           if bounds else None}
    if not bounds:
        return out
    a = pe.signal_a(v, ct, prep.count, bg["mode"], bounds, levels=LEVELS)
    out["A_table"] = {k: a.get(k) for k in ("gate", "candidate", "gates", "efficiency",
                                             "efficiency_se", "validity", "null")}
    if a.get("p") is not None:
        out["A_table"]["tiles"] = pe.tile_agreement(v, a["p"], prep.xy, bounds)
        raw = pe.pixel_gate(v, a["p"], bounds, normalise=False)
        out["A_table_raw"] = {"gate": raw["gate"], "candidate": raw["candidate"],
                              "validity": raw["validity"]}
    out["onset_table"] = pe.peak_onset(v, ct, bounds, bg["mode"])
    if cp is not None:
        ap = pe.signal_a(v, cp, prep.count, bg["mode"], bounds, levels=LEVELS)
        out["A_pixel"] = {k: ap.get(k) for k in ("gate", "candidate", "gates", "efficiency",
                                                  "efficiency_se", "validity")}
        out["onset_pixel"] = pe.peak_onset(v, cp, bounds, bg["mode"])
    # The engine's own numbers for this marker, from its newest session.
    latest = [u for _sid, _t, units in sessions for m, u in units.items() if m == marker]
    if latest:
        u = latest[-1]
        out["engine"] = {"start": (u.get("start") or {}).get("low"),
                         "otsu": _lattice_point(u, "otsu"), "gmm": _lattice_point(u, "gmm"),
                         "finals": [x.get("final") for x in latest if x.get("final") is not None],
                         "partners": [p for p in ((u.get("scoring") or {}).get("partners") or ())
                                      if p.get("relation") == "exclusive"]}
    return out


def signal_b(prep, marker, sessions, session_gates=None):
    """Signal B with partners from the newest session that has the marker
    and partner gates from that same session's finals (the engine's own)."""
    v, _ct, _cp, nbm = prep.column(marker)
    latest = None
    for sid, _t, units in sessions:
        if marker in units:
            latest = (sid, units)
    if latest is None:
        return None
    sid, units = latest
    u = units[marker]
    partners = []
    for p in ((u.get("scoring") or {}).get("partners") or ()):
        if p.get("relation") != "exclusive" or p.get("marker") not in prep.markers:
            continue
        pu = units.get(p["marker"])
        gate = (session_gates or {}).get(p["marker"]) if session_gates else \
            (pu.get("final") if pu else None)
        if gate is None:
            continue
        pv, _a, _b, _c = prep.column(p["marker"])
        partners.append((pv, float(gate)))
    if not partners:
        return {"value": None, "validity": {"valid": False, "reasons": ["no gated exclusive partner"]}}
    out = pe.exclusive_background(v, nbm, partners)
    out["session"] = sid
    return out


def _err(g, t):
    return None if g is None or t is None else abs(float(g) - float(t))


def _summ(errs):
    e = np.array([x for x in errs if x is not None], dtype=np.float64)
    if not e.size:
        return {"n": 0}
    return {"n": int(e.size), "mae": round(float(e.mean()), 3),
            "median": round(float(np.median(e)), 3),
            "within_0.2": round(float((e <= 0.2 + 1e-9).mean()), 3),
            "worst": round(float(e.max()), 3)}


def cmd_e2(args):
    truth_all = json.loads(Path(args.truth).read_text())
    report = {"images": {}, "cohorts": {}}
    for folder in args.prepared:
        prep = Prepared(folder)
        truth = truth_all.get(prep.image) or {}
        sessions = load_sessions(args.sessions, prep.image)
        rows = []
        for marker in prep.markers:
            t = truth.get(marker)
            v = prep.column(marker)[0]
            if not _is_gate(t, v):
                continue
            t0 = time.time()
            ev = marker_evidence(prep, marker, sessions)
            if ev is None:
                continue
            ev["truth"] = float(t)
            ev["B"] = signal_b(prep, marker, sessions)
            rows.append(ev)
            a = ev.get("A_table") or {}
            print(f"{prep.image:16} {marker:10} truth {t:5.2f}  A {a.get('gate')}  "
                  f"cand {a.get('candidate')}  eff {a.get('efficiency') and round(a['efficiency'], 2)}  "
                  f"{';'.join(a.get('validity', {}).get('reasons', []))}  ({time.time() - t0:.0f}s)",
                  flush=True)
        report["images"][prep.image] = {"cohort": prep.meta["cohort"], "rows": rows}
    Path(args.out).write_text(json.dumps(report, indent=1, default=float))
    print_e2(report)


def _methods(row):
    eng = row.get("engine") or {}
    finals = eng.get("finals") or []
    out = {
        "A_table": (row.get("A_table") or {}).get("gate"),
        "A_table@0.25": ((row.get("A_table") or {}).get("gates") or {}).get("0.25")
        if (row.get("A_table") or {}).get("gate") is not None else None,
        "A_table@0.1": ((row.get("A_table") or {}).get("gates") or {}).get("0.1")
        if (row.get("A_table") or {}).get("gate") is not None else None,
        "A_table_raw": (row.get("A_table_raw") or {}).get("gate"),
        "A_pixel": (row.get("A_pixel") or {}).get("gate"),
        "onset_table": (row.get("onset_table") or {}).get("onset"),
        "onset_pixel": (row.get("onset_pixel") or {}).get("onset"),
        "scored_start": eng.get("start"),
        "otsu": eng.get("otsu"),
        "mixture": eng.get("gmm"),
        "engine_latest": finals[-1] if finals else None,
    }
    return out


def print_e2(report):
    for image, rep in report["images"].items():
        rows = rep["rows"]
        print(f"\n== {image} ({rep['cohort']}): {len(rows)} markers with a person's gate")
        names = list(_methods(rows[0]).keys()) if rows else []
        for name in names:
            errs = [_err(_methods(r)[name], r["truth"]) for r in rows]
            cov = sum(e is not None for e in errs)
            print(f"  {name:14} coverage {cov:2}/{len(rows)}  {_summ(errs)}")
        # Paired, on the markers the pixel gate covers.
        cov = [r for r in rows if _methods(r)["A_table"] is not None]
        print(f"  -- paired on the {len(cov)} markers A_table covers:")
        for name in names:
            pairs = [(_err(_methods(r)["A_table"], r["truth"]), _err(_methods(r)[name], r["truth"]))
                     for r in cov]
            pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
            if not pairs:
                continue
            a = np.array([p[0] for p in pairs]); b = np.array([p[1] for p in pairs])
            print(f"     A {a.mean():.3f} vs {name:14} {b.mean():.3f}  (n {len(pairs)}, "
                  f"A better {int((a < b - 1e-9).sum())}, worse {int((a > b + 1e-9).sum())}, "
                  f"A >0.30 worse on {int((a - b > 0.30).sum())})")



def _unit_bounds(unit):
    h = unit.get("hypotheses") or {}
    b = (h.get("crowding") or {}).get("bounds") or [p["low"] for p in (h.get("points") or ())]
    return sorted(float(x) for x in b)


def cmd_e3(args):
    """E3 (tie-break replay) and E3b (checks) on the stored sessions."""
    truth_all = json.loads(Path(args.truth).read_text())
    e2 = json.loads(Path(args.e2).read_text()) if args.e2 else {"images": {}}
    report = {"replay": [], "checks": []}
    for folder in args.prepared:
        prep = Prepared(folder)
        truth = truth_all.get(prep.image) or {}
        e2rows = {r["marker"]: r for r in (e2["images"].get(prep.image) or {}).get("rows", [])}
        cache = {}
        for sid, _t, units in load_sessions(args.sessions, prep.image):
            for marker, u in units.items():
                if marker not in prep.markers or marker not in truth:
                    continue
                v, ct, _cp, _nb = prep.column(marker)
                t = float(truth[marker])
                if not _is_gate(t, v) or u.get("final") is None:
                    continue
                h = u.get("hypotheses") or {}
                post = ((h.get("support") or {}).get("posterior") or {})
                plateau = post.get("plateau") or []
                if len(plateau) >= 2:
                    bounds = _unit_bounds(u)
                    key = (marker, tuple(bounds))
                    if key not in cache:
                        bg = _background(v)
                        a = pe.signal_a(v, ct, prep.count, bg["mode"], bounds) if bg else None
                        cache[key] = a
                    a = cache[key]
                    likeliest = max(plateau, key=lambda m: (m["p"], -m["low"]))["low"]
                    chosen, why = likeliest, "invalid: likeliest kept"
                    if a and a.get("loss") and a["validity"]["valid"]:
                        members = [m for m in plateau if m["index"] < len(a["loss"])]
                        best = min(members, key=lambda m: (a["loss"][m["index"]], m["low"]))
                        chosen, why = best["low"], "pixel loss"
                    report["replay"].append({
                        "image": prep.image, "session": sid, "marker": marker, "truth": t,
                        "written": float(u["final"]), "likeliest": float(likeliest),
                        "pixel": float(chosen), "why": why,
                        "plateau": [m["low"] for m in plateau],
                        "valid": bool(a and a["validity"]["valid"]),
                        "reasons": (a or {}).get("validity", {}).get("reasons")})
                # E3b: the checks on gates the engine accepted.
                state = u.get("state") or ""
                row = e2rows.get(marker) or {}
                agate = (row.get("A_table") or {}).get("gate")
                bval = (row.get("B") or {}).get("value")
                final = float(u["final"])
                report["checks"].append({
                    "image": prep.image, "session": sid, "marker": marker, "truth": t,
                    "final": final, "state": state, "error": abs(final - t),
                    "accepted": state.startswith("accepted"),
                    "review": state == "manual_review_recommended",
                    "A_fires": agate is not None and agate < final - pe.TOLERANCE,
                    "B_fires": bval is not None and final < bval - pe.TOLERANCE})
    Path(args.out).write_text(json.dumps(report, indent=1, default=float))
    print_e3(report)
    print_priors(report, e2)


def print_e3(report):
    rep = report["replay"]
    for image in sorted({r["image"] for r in rep}):
        rows = [r for r in rep if r["image"] == image]
        acted = [r for r in rows if r["why"] == "pixel loss"]
        print(f"\n== E3 {image}: {len(rows)} unresolved closes with a plateau, "
              f"signal valid on {len(acted)}")
        for name in ("written", "likeliest", "pixel"):
            print(f"  {name:10} all {_summ([_err(r[name], r['truth']) for r in rows])}")
        if acted:
            a = np.array([_err(r["pixel"], r["truth"]) for r in acted])
            b = np.array([_err(r["likeliest"], r["truth"]) for r in acted])
            print(f"  where it acts: pixel {a.mean():.3f} vs likeliest {b.mean():.3f} "
                  f"(better {int((a < b - 1e-9).sum())}, worse {int((a > b + 1e-9).sum())}, "
                  f">0.30 worse {int((a - b > 0.30).sum())})")
    checks = report["checks"]
    for image in sorted({r["image"] for r in checks}):
        rows = [r for r in checks if r["image"] == image]
        acc = [r for r in rows if r["accepted"]]
        bad = lambda rs: sum(r["error"] > pe.TOLERANCE for r in rs)
        print(f"\n== E3b {image}: {len(rows)} written gates, {len(acc)} accepted, "
              f"{bad(acc)} accepted > tolerance off (base rate "
              f"{bad(acc) / max(len(acc), 1):.2f})")
        rev = [r for r in rows if r["review"]]
        print(f"  engine review flag: {len(rev)} flagged, precision "
              f"{bad(rev) / max(len(rev), 1):.2f}")
        for name in ("A_fires", "B_fires"):
            fired = [r for r in acc if r[name]]
            print(f"  {name}: fires on {len(fired)} accepted gates, precision "
                  f"{bad(fired) / max(len(fired), 1):.2f}, demotes {len(fired) - bad(fired)} good gates")



# -- E4 (exploratory): the contrast as a reader of the cells a look showed -----------------

#: Contrast thresholds chosen on the person-labelled cell sets with the
#: image's own platform left out (`e1 --rows`, then `loso`): never on the
#: image they are used on. Keys: the label set left out.
LOSO_THRESHOLDS = {"table": {"lsp11385_2": -0.60, "lsp35766_p193_2": -0.75},
                   "pixel": {"lsp11385_2": -0.45, "lsp35766_p193_2": -0.75}}


def _decide(h, labels=None):
    import copy

    from plexora.plugins.gating.server.autogate import hypotheses
    from plexora.plugins.gating.server.autogate.hypotheses import controller

    state = copy.deepcopy(h)
    for key, value in (("n_finite", 0), ("grown", {"up": 0, "down": 0}), ("split", [])):
        state.setdefault(key, value)
    state["params"] = {**hypotheses.PARAMS, **(state.get("params") or {})}
    if labels is not None:
        for obs in state.get("observations") or ():
            for cell in obs.get("cells") or ():
                new = labels(cell)
                if new is not None:
                    cell["label"] = new
    status, s = controller.decide(state, float, state["params"])
    return status, (None if s is None else float(s["low"]))


def cmd_e4(args):
    truth_all = json.loads(Path(args.truth).read_text())
    rows = []
    for folder in args.prepared:
        prep = Prepared(folder)
        truth = truth_all.get(prep.image) or {}
        t_c = LOSO_THRESHOLDS[args.version][prep.image]
        for sid, _t, units in load_sessions(args.sessions, prep.image):
            for marker, u in units.items():
                h = u.get("hypotheses") or {}
                if not h.get("observations") or marker not in truth or marker not in prep.markers:
                    continue
                v, ct, cp, _nb = prep.column(marker)
                if not _is_gate(float(truth[marker]), v):
                    continue
                c = cp if (args.version == "pixel" and cp is not None) else ct

                def call(cell, only_uncertain):
                    if cell.get("sentinel"):
                        return None
                    if only_uncertain and cell.get("label") != "uncertain":
                        return None
                    i = int(cell["cell_id"]) - 1
                    if i < 0 or i >= c.shape[0] or not np.isfinite(c[i]):
                        return None
                    return "own_stain" if c[i] > t_c else "not_own_stain"

                try:
                    base = _decide(h)
                    unc = _decide(h, lambda cell: call(cell, True))
                    det = _decide(h, lambda cell: call(cell, False))
                except Exception as exc:  # a stored state this code cannot decide
                    print(f"  skip {sid} {marker}: {type(exc).__name__}: {exc}", flush=True)
                    continue
                labels = [cell.get("label") for o in h["observations"] for cell in o.get("cells") or ()
                          if not cell.get("sentinel")]
                rows.append({"image": prep.image, "session": sid, "marker": marker,
                             "truth": float(truth[marker]), "stored": u.get("final"),
                             "base": base[1], "base_status": base[0],
                             "uncertain_to_contrast": unc[1], "unc_status": unc[0],
                             "contrast_only": det[1], "det_status": det[0],
                             "reads": len(labels),
                             "uncertain": sum(lab == "uncertain" for lab in labels)})
    Path(args.out).write_text(json.dumps(rows, indent=1, default=float))
    for image in sorted({r["image"] for r in rows}):
        rs = [r for r in rows if r["image"] == image]
        print(f"\n== E4 {image} ({args.version}, threshold {LOSO_THRESHOLDS[args.version][image]}): "
              f"{len(rs)} units, {len({r['marker'] for r in rs})} markers, uncertain share "
              f"{sum(r['uncertain'] for r in rs) / max(sum(r['reads'] for r in rs), 1):.2f}")
        for name in ("base", "uncertain_to_contrast", "contrast_only"):
            errs = [_err(r[name], r["truth"]) for r in rs]
            mk = sorted({r["marker"] for r in rs})
            pm = np.mean([np.mean([_err(r[name], r["truth"]) for r in rs if r["marker"] == m
                                   and r[name] is not None] or [np.nan]) for m in mk])
            print(f"  {name:22} {_summ(errs)}  per-marker {pm:.3f}")
        for name, status in (("uncertain_to_contrast", "unc_status"),
                             ("contrast_only", "det_status")):
            pairs = [(_err(r[name], r["truth"]), _err(r["base"], r["truth"])) for r in rs
                     if r[name] is not None and r["base"] is not None]
            a = np.array([x for x, _ in pairs])
            b = np.array([y for _, y in pairs])
            print(f"  {name:22} vs base: better {int((a < b - 1e-9).sum())}, worse "
                  f"{int((a > b + 1e-9).sum())}, >0.30 worse {int((a - b > 0.30).sum())}; "
                  f"resolved {sum(r[status] == 'resolved' for r in rs)} "
                  f"vs base {sum(r['base_status'] == 'resolved' for r in rs)}")



def print_priors(report, e2):
    """What a tie-break could gain at most (the plateau member nearest the
    person's gate), and what choosing the member nearest each deterministic
    estimate gives instead."""
    est = {}
    for image, rep in (e2.get("images") or {}).items():
        for r in rep.get("rows", []):
            eng = r.get("engine") or {}
            est[(image, r["marker"])] = {
                "scored start": eng.get("start"), "mixture": eng.get("gmm"),
                "Otsu": eng.get("otsu"), "B": (r.get("B") or {}).get("value"),
                "local-peak onset (table)": (r.get("onset_table") or {}).get("onset"),
                "local-peak onset (pixel)": (r.get("onset_pixel") or {}).get("onset"),
                "background onset": r["background"].get("onset"),
                "background ceiling": r["background"].get("ceiling")}
    names = ["likeliest", "written", "scored start", "mixture", "Otsu", "B",
             "local-peak onset (table)", "local-peak onset (pixel)", "background onset",
             "background ceiling"]
    for image in sorted({r["image"] for r in report["replay"]}):
        rows = [r for r in report["replay"] if r["image"] == image]
        oracle = np.array([min(abs(m - r["truth"]) for m in r["plateau"]) for r in rows])
        print(f"\n== tie-break priors {image}: {len(rows)} closes; oracle member "
              f"{oracle.mean():.3f}; person's gate within 0.2 of some member "
              f"{(oracle <= 0.2 + 1e-9).mean():.2f}")
        for name in names:
            errs = []
            for r in rows:
                if name in ("likeliest", "written"):
                    g = r[name]
                else:
                    x = est.get((image, r["marker"]), {}).get(name)
                    g = r["likeliest"] if x is None else \
                        min(r["plateau"], key=lambda m: (abs(m - x), m))
                errs.append(abs(g - r["truth"]))
            print(f"  nearest {name:26} {np.mean(errs):.3f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("e1")
    p.add_argument("sets", nargs="+")
    p.add_argument("--out", required=True)
    p.add_argument("--rows", action="store_true")
    p.set_defaults(func=cmd_e1)
    p = sub.add_parser("prepare")
    p.add_argument("--image", required=True, choices=sorted(IMAGES))
    p.add_argument("--out", required=True)
    p.add_argument("--pixel", action="store_true")
    p.add_argument("--level", default=1)
    p.add_argument("--markers", default=None)
    p.set_defaults(func=cmd_prepare)
    p = sub.add_parser("e2")
    p.add_argument("--prepared", nargs="+", required=True)
    p.add_argument("--truth", required=True)
    p.add_argument("--sessions", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_e2)
    p = sub.add_parser("e3")
    p.add_argument("--prepared", nargs="+", required=True)
    p.add_argument("--truth", required=True)
    p.add_argument("--sessions", required=True)
    p.add_argument("--e2", default=None)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_e3)
    p = sub.add_parser("e4")
    p.add_argument("--prepared", nargs="+", required=True)
    p.add_argument("--truth", required=True)
    p.add_argument("--sessions", required=True)
    p.add_argument("--version", default="table", choices=("table", "pixel"))
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_e4)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
