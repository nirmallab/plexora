"""Per-cell calls from a small image model: is this cell's stain its own?

A gate is one number for the whole image; a cell just above it may be lit
by a bright neighbour, and one just below may carry a real dim stain. A
small cell-aware CNN (`cellcall_weights`) reads each cell's crop -- the
marker, the DNA, the segmentation, up to two expected and two not-expected
partner channels -- and answers one question: the probability the stain is
the cell's own.

The rule its calls follow (`decide`): **the gate's call stands unless the
model is confident** -- `p_own >= 1 - confident` makes a cell positive,
`p_own <= confident` negative. A cell where the model leans against the
gate without being confident keeps the gate's call and is `uncertain`. On
the expert's labelled cells of lsp11385_2 (an image the shipped weights
were validated on with it held out), at a live session's gates: 63 % ->
80 % agreement near the gate, 77 % -> 85 % far from it; letting the model
call every cell was WORSE far from the gate (60 %), where its unconfident
calls were wrong three times in four (docs/internal/bench/runs/
2026-10-10-cellcall-model). Validation pending beyond that image.

Never applied to the data: the gate, the expression matrix, `calls()`,
counts and exports stay the gate's. This is a layer beside them, shown and
written to its own place.

How a marker is read (the model's input contract, `CHANNELS`):
  * crops of `crop_radii` cell radii a side, read at full resolution, cut to
    the central `centre_radii` and resampled to `side` px -- cells are the
    same size in every image, so no pixel size is needed;
  * channel 0 is the marker over its PIXEL gate T: the cell-mask mean that
    best reproduces the table's calls on isolated cells near the gate, so
    the table's units (log or not, mean or sum) never reach the model;
  * channels 1, 2, 6-9 are image-relative: (x - p50) / (p99 - p50), signed
    log, the levels pooled over gate-independent reference cells;
  * channels 3-5 are the cell's mask, its neighbours' and their edges.
The model runs on the cells within `band_halves` band halves of the gate,
nearest first, at most `max_cells` (outside, the gate's call stands).

Two halves: `select` runs where the table is (a registered table op: the
cells to read, their calls, the partners' abundance), `predict` where the
pixels are. Results are cached on disk per gate, model and data.
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import zlib

import numpy as np

from plexora.plugins.gating.server.autogate import schemas

CELLCALL_VERSION = schemas.CELLCALL_VERSION

#: The model's input contract (`cellcall_weights.MODEL_LAYOUT`).
CHANNELS = ("m_gate", "m_img", "dna", "own", "others", "edges",
            "co1", "co2", "anti1", "anti2", "has_co", "has_anti")

CELLCALL = {
    # the model's contract: what it was trained on, never tuned
    "crop_radii": 12, "centre_radii": 8, "side": 48, "partners_per_kind": 2,
    "calibration_halves": 2.0, "calibration_cells": 200, "calibration_min_px": 6,
    "reference_cells": 20, "reference_band_cells": 44,
    "reference_bright_pct": 90.0, "reference_dim_pct": 40.0, "levels_every_px": 4,
    "pixel_gate_fallback": 0.25,
    # [cal] the calling rule, from the far-cell labels of one image
    # (0.1-0.2 scored the same); validation pending
    "confident": 0.1,
    # [cal] where the model runs: nearest the gate first, under a cap that
    # bounds a marker to about three minutes on a laptop (reads ~30-45 s
    # whatever the count, ~2 ms a cell after). At a live session's gates on
    # lsp11385_2, 40,000 cells within 6 halves left 19 confident calls unread
    # (18 right); 100,000 within 8 reach >= ~4.5 halves for every marker
    "band_halves": 8.0, "max_cells": 100_000,
    # memory: one read covers this many pyramid tiles' cells (one block per
    # channel each, ~6 MB a channel at 1024 px tiles), at most `read_ahead`
    # reads wait, and the model sees this many crops at a time
    "tiles_per_read": 2, "batch_cells": 512, "read_ahead": 2,
    # speed: crops are cut into tensors and run through the model on this
    # many threads at once, each with its own onnxruntime session of
    # cores / workers threads (0: a quarter of the cores, at most two). The
    # model alone runs at 1.1-1.5 ms a cell however the cores are split (it
    # is compute bound); the gain is the tensors off the one thread. On an M2
    # Pro (10 cores), 40,000 cells of lsp11385_2: 433 cells/s with one worker,
    # 528 with two, 455 with four (`plexora ai bench cellcall`)
    "workers": 0,
    # how many runs of a marker are kept on disk (newest first): a session
    # reads near a provisional gate, then again where a look moved it
    "keep_runs": 3,
}

#: The entries of `CELLCALL` that change how fast a run goes, never what it
#: says: left out of the stored runs' keys.
RUNTIME_ONLY = ("tiles_per_read", "batch_cells", "read_ahead", "workers", "keep_runs")

#: The vocabulary's confidence words, ranked.
_CONFIDENCE = {"high": 3, "moderate": 2, "low": 1}


class Unavailable(RuntimeError):
    """The model cannot call this marker; `str()` says why, in words."""


# -- the table's half ----------------------------------------------------------------


def _rng(marker, seed):
    return np.random.default_rng([int(seed), zlib.crc32(str(marker).encode())])


def select(ds, marker, low, high=None, *, gates=None, pixel_um=None, seed=0,
           band_halves=None, max_cells=None) -> dict:
    """What `predict` reads for `marker` at the gate `low`: every list holds
    table rows with their `ids`/`xs`/`ys` beside them (`cells`).

      run          eligible cells within `band_halves` of the gate (the
                   argument, else `CELLCALL`'s), nearest first, at most
                   `max_cells`; `run_call` their gate calls
                   (`cell_exclusions.calls`), `run_halves` their distance and
                   `run_vf` their values in the fit space (the gate is
                   `gate_fit` there), so a run can be read at another gate
      calibration  up to `calibration_cells` isolated cells (no strong
                   positive touching) within `calibration_halves`, with
                   `calibration_pos` (above the gate)
      references   gate-INDEPENDENT cells the image levels are pooled over:
                   the top decile and bottom `reference_dim_pct` %, each
                   isolated and touching (`reference_cells` apiece), plus
                   `reference_band_cells` near the gate
      abundance    {marker: share positive} for every gated marker in `gates`
    """
    from plexora.agent import cell_exclusions
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import profile as profmod
    from plexora.plugins.gating.server.autogate import neighbourhood, sampler

    cfg = CELLCALL
    rng = _rng(marker, seed)
    feats = neighbourhood.of(ds, marker, pixel_um=pixel_um)
    col = profmod.column(ds, marker)
    _edges, h = sampler.stratum_edges(profmod.fit_for(ds, marker), col, float(low))
    gf = float(col.to_fit(float(low)))
    vf = np.asarray(feats.vf, dtype=np.float64)
    v = cellmod.values(ds, marker)
    elig = feats.eligible & np.isfinite(vf) & np.isfinite(v)
    if not (np.isfinite(h) and h > 0) or not elig.any():
        raise Unavailable("the marker has no spread of values to read a gate against")
    touch = feats.nearest_strong <= feats.adjacent_px
    halves = np.abs(vf - gf) / h
    halves[~elig] = np.inf

    reach = float(cfg["band_halves"] if band_halves is None else band_halves)
    cap = int(cfg["max_cells"] if max_cells is None else max_cells)
    within = np.flatnonzero(halves <= reach)
    run = within[np.argsort(halves[within], kind="stable")][:cap]
    # A gate with no upper bound is open above (`gate_rule.passes` reads a
    # missing one as NaN, which nothing is at or below).
    calls = cell_exclusions.calls(ds, marker, v, float(low), np.inf if high is None else float(high))

    band = elig & (halves <= cfg["calibration_halves"])
    iso = np.flatnonzero(band & ~touch)
    calibration = np.sort(rng.choice(iso, size=min(int(cfg["calibration_cells"]), iso.size),
                                     replace=False)) if iso.size else iso
    dim, bright = np.nanpercentile(v[elig], [cfg["reference_dim_pct"], cfg["reference_bright_pct"]])
    references = []
    for mask in (elig & (v >= bright) & ~touch, elig & (v >= bright) & touch,
                 elig & (v <= dim) & ~touch, elig & (v <= dim) & touch):
        rows = np.flatnonzero(mask)
        if rows.size:
            references += rng.choice(rows, size=min(int(cfg["reference_cells"]), rows.size),
                                     replace=False).tolist()
    rows = np.flatnonzero(band)
    if rows.size:
        references += rng.choice(rows, size=min(int(cfg["reference_band_cells"]), rows.size),
                                 replace=False).tolist()

    abundance = {}
    for name, gate in (gates or {}).items():
        if name == marker or name not in ds.table.markers or gate is None:
            continue
        values = cellmod.values(ds, name)
        abundance[name] = float(np.nanmean(values > float(gate)))

    c = cellmod.cells(ds)
    wanted = np.unique(np.concatenate([run, calibration, np.asarray(references, dtype=np.int64)]))
    return {
        "marker": marker, "low": float(low), "high": None if high is None else float(high),
        "n": int(c.n), "gate_fit": gf, "band_half": float(h),
        "run": run.tolist(), "run_call": calls[run].tolist(),
        "run_halves": [round(float(x), 3) for x in halves[run]],
        "run_vf": [float(x) for x in vf[run].astype(np.float32)],
        "band_halves": reach, "max_cells": cap,
        "n_within": int(within.size), "capped": bool(within.size > run.size),
        "calibration": calibration.tolist(),
        "calibration_pos": (vf[calibration] > gf).tolist(),
        "references": [int(r) for r in references],
        "abundance": abundance,
        "cells": {"rows": wanted.tolist(), "ids": c.ids[wanted].tolist(),
                  "xs": c.xs[wanted].tolist(), "ys": c.ys[wanted].tolist()},
    }


# -- partners -----------------------------------------------------------------------


def partner_plan(entries, marker, present, abundance, n, gated=()):
    """[{marker, kind: co|anti, relation, confidence, steps}]: up to `n`
    channels expected in the same cell and `n` not expected, from the panel
    context's vocabulary relations (`entries[m]["partners"]`, mapped to this
    panel's names) -- the rule the labelling pages the model learned from
    used, so the same partners reach it:
      co    the marker's own subset / coexpressed partners (a relation
            another marker declares about this one is not reversed), then the
            supersets of its supersets; the most specific first;
      anti  exclusive partners either way, those of the marker's supersets,
            and the subsets of a marker it excludes.
    A derived relation is as confident as its weakest step. A partner the
    scientist gated comes first (their gate vouches for the channel), then
    confidence, then the partner more cells are positive for (`abundance`).
    One partner per vocabulary entry: two channels read as the same marker
    say one thing twice."""
    rel = {}
    for name, entry in entries.items():
        for p in (entry or {}).get("partners") or []:
            rel[(name, p["marker"])] = (p["relation"], _CONFIDENCE.get(p.get("confidence"), 1))

    def ups(a):
        return {b: conf for (x, b), (r, conf) in rel.items() if x == a and r == "subset"}

    def downs(a):
        return {x: conf for (x, b), (r, conf) in rel.items() if b == a and r == "subset"}

    def excl(a):
        out = {}
        for (x, b), (r, conf) in rel.items():
            if r == "exclusive" and a in (x, b):
                other = b if x == a else x
                out[other] = max(out.get(other, 0), conf)
        return out

    co = {}
    for (x, b), (r, conf) in rel.items():
        if x == marker and r in ("subset", "coexpressed"):
            co[b] = (conf, 1, r)
    supers = dict(ups(marker))
    for b, conf in list(supers.items()):
        for b2, conf2 in ups(b).items():
            if b2 != marker:
                supers.setdefault(b2, min(conf, conf2))
                co.setdefault(b2, (min(conf, conf2), 2, "subset"))
    anti = {b: (conf, 1, "exclusive") for b, conf in excl(marker).items()}
    for s_, conf in supers.items():
        for b, conf2 in excl(s_).items():
            anti.setdefault(b, (min(conf, conf2), 2, "exclusive"))
    for b, (conf, steps, _r) in list(anti.items()):
        for sub, conf2 in downs(b).items():
            anti.setdefault(sub, (min(conf, conf2), steps + 1, "exclusive"))
    for table in (co, anti):
        for name in [k for k in table if k == marker or k not in present]:
            del table[name]
    for name in list(anti):
        if name in co:
            del anti[name]

    def rank(table, specific):
        def key(name):
            conf, steps, _r = table[name]
            broader = specific and any(name in ups(other) for other in table if other != name)
            return (name not in gated, broader, -conf, steps, -(abundance.get(name) or 0.0), name)
        return sorted(table, key=key)[:n]

    def dedupe(table):
        seen = {}
        for name in sorted(table, key=lambda k: (
                (entries.get(k) or {}).get("canonical") != k, -table[k][0], table[k][1],
                -(abundance.get(k) or 0.0), k)):
            seen.setdefault((entries.get(name) or {}).get("canonical") or name, name)
        for name in [k for k in table if k not in seen.values()]:
            del table[name]
    dedupe(co)
    dedupe(anti)

    words = {v: k for k, v in _CONFIDENCE.items()}
    return [{"marker": name, "kind": kind, "relation": table[name][2],
             "confidence": words[table[name][0]], "steps": table[name][1]}
            for kind, table, specific in (("co", co, True), ("anti", anti, False))
            for name in rank(table, specific)]


# -- tensors ------------------------------------------------------------------------


def best_cut(values, calls):
    """(cut, agreement): the value above which `values` best reproduce the
    bool `calls` (NaN under 10 values)."""
    values = np.asarray(values, dtype=np.float64)
    calls = np.asarray(calls, dtype=bool)
    if values.size < 10:
        return float("nan"), 0.0
    best, acc = None, -1.0
    for t in np.unique(values):
        a = float(((values > t) == calls).mean())
        if a > acc:
            best, acc = float(t), a
    return best, round(acc, 3)


def _centre_window(n):
    k = int(round(n * CELLCALL["centre_radii"] / CELLCALL["crop_radii"]))
    return k, (n - k) // 2


def _coordinates(k):
    """Where each output pixel samples the k-px window: `scipy.ndimage.zoom`'s
    own arithmetic (corner-aligned, the zoom factor computed first, a
    coordinate past the edge reading 0), so a crop resampled here is the
    crop the model was trained on, bit for bit."""
    side = CELLCALL["side"]
    out = int(round(k * (side / k)))
    step = (k - 1) / (out - 1) if out > 1 else 0.0
    x = np.arange(side, dtype=np.float64) * step
    return x, x <= k - 1


def _linear_matrix(k):
    x, inside = _coordinates(k)
    i0 = np.minimum(np.floor(x).astype(np.int64), k - 1)
    i1 = np.minimum(i0 + 1, k - 1)
    w = x - i0
    out = np.zeros((x.size, k))
    rows = np.flatnonzero(inside)
    np.add.at(out, (rows, i0[rows]), 1.0 - w[rows])
    np.add.at(out, (rows, i1[rows]), w[rows])
    return out


def resample(planes):
    """(n, side, side) float32 from (n, s, s) crops: the central window,
    linearly resampled."""
    planes = np.asarray(planes, dtype=np.float32)
    k, o = _centre_window(planes.shape[-1])
    window = planes[..., o:o + k, o:o + k]
    if k == CELLCALL["side"]:
        return window.astype(np.float32)
    m = _linear_matrix(k)
    return np.matmul(np.matmul(m, window.astype(np.float64)), m.T).astype(np.float32)


def resample_labels(labels):
    """(n, side, side) labels, nearest neighbour (0 past the edge)."""
    labels = np.asarray(labels)
    k, o = _centre_window(labels.shape[-1])
    window = labels[..., o:o + k, o:o + k]
    if k == CELLCALL["side"]:
        return window
    x, inside = _coordinates(k)
    index = np.minimum(np.floor(x + 0.5).astype(np.int64), k - 1)
    out = window[..., index[:, None], index[None, :]]
    return out * np.outer(inside, inside).astype(out.dtype)


def slog(x):
    return np.sign(x) * np.log1p(np.abs(x))


def tensors(crops, cell_ids, marker, *, pixel_gate, levels, co=(), anti=()):
    """(kept cell ids, (n, 12, side, side) float32) in `CHANNELS` order.

    `crops` {cell_id: Crop} with planes under their channel names (the DNA as
    `__nuclear__`) and labels; `levels` {name: (p50, p99 - p50)}; `co` /
    `anti` the partner names (at most two each). Cells without a mask crop
    or a marker plane are dropped."""
    from scipy import ndimage as ndi

    side = CELLCALL["side"]
    kept = [cid for cid in cell_ids
            if (cr := crops.get(int(cid))) is not None and cr.labels is not None
            and cr.planes.get(marker) is not None]
    x = np.zeros((len(kept), len(CHANNELS), side, side), np.float32)
    if not kept:
        return kept, x

    def stack(name):
        return np.stack([crops[int(cid)].planes[name] for cid in kept])

    m = resample(stack(marker))
    x[:, 0] = np.log1p(np.clip(m, 0, None) / float(pixel_gate))
    b, s = levels[marker]
    x[:, 1] = slog((m - b) / s)
    if "__nuclear__" in levels and all("__nuclear__" in crops[int(c)].planes for c in kept):
        b, s = levels["__nuclear__"]
        x[:, 2] = slog((resample(stack("__nuclear__")) - b) / s)
    labels = resample_labels(np.stack([np.asarray(crops[int(c)].labels).astype(np.int64)
                                       for c in kept]))
    own = labels == np.asarray(kept, dtype=np.int64)[:, None, None]
    x[:, 3] = own
    x[:, 4] = (labels != 0) & ~own
    x[:, 5] = ndi.maximum_filter(labels, size=(1, 3, 3)) != ndi.minimum_filter(labels, size=(1, 3, 3))
    for j, name in enumerate(list(co)[:2]):
        b, s = levels[name]
        x[:, 6 + j] = slog((resample(stack(name)) - b) / s)
    for j, name in enumerate(list(anti)[:2]):
        b, s = levels[name]
        x[:, 8 + j] = slog((resample(stack(name)) - b) / s)
    x[:, 10] = float(bool(co))
    x[:, 11] = float(bool(anti))
    return kept, x


def own_mean(crop, marker):
    """The mean of the cell's own mask pixels, or None (no mask, under
    `calibration_min_px` pixels)."""
    if crop is None or crop.labels is None or crop.planes.get(marker) is None:
        return None
    plane = np.asarray(crop.planes[marker], dtype=np.float64)
    labels = np.asarray(crop.labels)
    if labels.shape != plane.shape:
        return None
    own = labels == crop.cell_id
    if own.sum() < CELLCALL["calibration_min_px"]:
        return None
    return float(plane[own].mean())


# -- the pixels' half ---------------------------------------------------------------


class Predictor:
    """The ONNX model on onnxruntime's CPU provider: `(n, 12, s, s) -> (n,)`.
    `threads` 0 lets onnxruntime take every core. `identity` names the
    weights in run keys and records (`cellcall_weights.identity`)."""

    workers = 1

    def __init__(self, path, threads=0):
        import onnxruntime as ort

        from plexora.plugins.gating.server.autogate import cellcall_weights

        options = ort.SessionOptions()
        options.log_severity_level = 3
        if threads:
            options.intra_op_num_threads = int(threads)
            options.inter_op_num_threads = 1
        self.path = str(path)
        self.identity = cellcall_weights.identity(path)
        self.session = ort.InferenceSession(self.path, options, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        self.output = self.session.get_outputs()[0].name

    def __call__(self, x):
        if not len(x):
            return np.zeros(0, np.float32)
        return np.asarray(self.session.run([self.output], {self.input: x})[0], dtype=np.float32)


def default_workers() -> int:
    import os

    n = int(CELLCALL["workers"] or 0)
    if n > 0:
        return n
    return max(1, min(2, (os.cpu_count() or 4) // 4))


class Pool:
    """`workers` sessions of one model file, each on cores / workers threads,
    called from as many threads at once (one session each, borrowed per call)."""

    def __init__(self, path, workers=None):
        import os

        self.workers = int(workers or default_workers())
        threads = max(1, (os.cpu_count() or self.workers) // self.workers)
        self._all = [Predictor(path, threads=threads) for _ in range(self.workers)]
        self._free = queue.Queue()
        for one in self._all:
            self._free.put(one)
        self.path = self._all[0].path
        self.identity = self._all[0].identity

    def __call__(self, x):
        one = self._free.get()
        try:
            return one(x)
        finally:
            self._free.put(one)


_PREDICTORS: dict = {}


def predictor():
    """The model (`cellcall_weights.ensure`: the shipped file, fetched first
    if missing, or the unpinned development file), loaded once per process
    as a `Pool`; `cellcall_weights.CellModelMissing` when it cannot be had."""
    from plexora.plugins.gating.server.autogate import cellcall_weights

    path = cellcall_weights.ensure()
    key = (str(path), default_workers())
    if key not in _PREDICTORS:
        _PREDICTORS[key] = Pool(path)
    return _PREDICTORS[key]


def _channel_keys(ds):
    """{marker: record channel key} for every panel marker with an image channel."""
    from plexora.agent.render import resolve_channel
    from plexora.plugins.gating.server.autogate import views
    from plexora.server.utils import source_image

    channels = list(ds.project.image.real_channels)
    out = {}
    for name in ds.table.markers:
        channel = views.image_channel(ds, name)
        if channel is not None:
            out[name] = source_image.channel_key(resolve_channel(channel, channels)[1])
    return out


def _read(session, record, cells, rows, channels, crop_px):
    from plexora.agent.evidence import crops as cropmod

    found = [{"cell_id": cells[r][0], "x": cells[r][1], "y": cells[r][2]} for r in rows]
    crops, _info = cropmod.read_cell_crops(session, record, found, channels=channels,
                                           crop_px=crop_px, tile_px=crop_px, mask=True, level=0)
    return crops


def plan_for(session, ds, marker, *, abundance, gated):
    """(channels {name: key}, plan, nuclear name or None) -- what a marker's
    crops are read with."""
    from plexora.plugins.gating.server.autogate import context as ctxmod
    from plexora.plugins.gating.server.autogate import neighbourhood

    keys = _channel_keys(ds)
    if marker not in keys:
        raise Unavailable("the marker has no image channel")
    nuclear = neighbourhood.image_facts(session, ds).get("nuclear")
    nuclear_key = None
    if nuclear:
        from plexora.agent.render import resolve_channel
        from plexora.server.utils import source_image

        nuclear_key = source_image.channel_key(
            resolve_channel(nuclear, list(ds.project.image.real_channels))[1])
    present = {n for n, k in keys.items() if n != nuclear and k != nuclear_key}
    entries = ctxmod.for_project(ds, create=False).get("entries") or {}
    plan = partner_plan(entries, marker, present, abundance, CELLCALL["partners_per_kind"],
                        set(gated))
    channels = {marker: keys[marker]}
    if nuclear_key:
        channels["__nuclear__"] = nuclear_key
    for p in plan:
        channels[p["marker"]] = keys[p["marker"]]
    return channels, plan, nuclear


def predict(session, ds, selection, *, model=None, progress=None, cancelled=None) -> dict:
    """{rows, p, pixel_gate, levels, plan, ...} for a `select` result: the
    model's p_own for each `run` row it could read (in `run` order)."""
    from plexora.agent import render
    from plexora.agent.evidence import crops as cropmod
    from plexora.plugins.gating.server.autogate import neighbourhood

    cfg = CELLCALL
    record = ds.project
    facts = neighbourhood.image_facts(session, ds)
    if facts.get("blank"):
        raise Unavailable("the project has no image")
    if facts.get("brightfield"):
        raise Unavailable("a brightfield image: the model reads fluorescence channels")
    provider, _status, reason, _locator = render._mask_for(record)
    if provider is None:
        raise Unavailable(f"the model reads the segmentation mask, and there is none "
                          f"({reason or 'no mask'})")
    marker = selection["marker"]
    gated = set(selection.get("abundance") or {})
    channels, plan, _nuclear = plan_for(session, ds, marker,
                                        abundance=selection.get("abundance") or {}, gated=gated)
    model = model or predictor()
    t = selection["cells"]
    cells = {int(r): (int(i), float(x), float(y))
             for r, i, x, y in zip(t["rows"], t["ids"], t["xs"], t["ys"])}
    radius, _how = cropmod.cell_radius_px(record)
    crop_px = cfg["crop_radii"] * radius

    # the pixel gate: the cell-mask mean that best reproduces the table's
    # calls on isolated cells near the gate
    calibration = selection["calibration"]
    found = _read(session, record, cells, calibration, {marker: channels[marker]}, crop_px)
    means, pos = [], []
    for r, is_pos in zip(calibration, selection["calibration_pos"]):
        value = own_mean(found.get(cells[r][0]), marker)
        if value is not None:
            means.append(value)
            pos.append(bool(is_pos))
    pixel_gate, agreement = best_cut(np.array(means), np.array(pos))

    # image-relative levels over the gate-independent reference cells
    found = _read(session, record, cells, selection["references"], channels, crop_px)
    pools = {}
    every = int(cfg["levels_every_px"])
    for crop in found.values():
        for name, plane in crop.planes.items():
            pools.setdefault(name, []).append(np.asarray(plane, np.float32)[::every, ::every].ravel())
    levels = {}
    for name, parts in pools.items():
        p50, p99 = np.percentile(np.concatenate(parts), [50, 99])
        levels[name] = (float(p50), float(max(p99 - p50, 1e-6)))
    if marker not in levels:
        raise Unavailable("no reference cell of the marker could be read")
    if not np.isfinite(pixel_gate) or pixel_gate <= 0:
        b, s = levels[marker]
        pixel_gate = b + cfg["pixel_gate_fallback"] * s
    co = [p["marker"] for p in plan if p["kind"] == "co"][:2]
    anti = [p["marker"] for p in plan if p["kind"] == "anti"][:2]

    # the run cells, a few pyramid tiles at a time: one thread reads the next
    # tiles' crops while `workers` threads cut the last ones into tensors and
    # run the model (the decoders, numpy and onnxruntime let go of the GIL),
    # at most `read_ahead` chunks read ahead and one more than `workers` in
    # hand, so memory stays bounded whatever the count
    run = list(selection["run"])
    tile_w = int(record.image.tile_width or 1024)
    tile_h = int(record.image.tile_height or 1024)
    tiles = {}
    for r in run:
        _cid, x, y = cells[r]
        tiles.setdefault((int(y // tile_h), int(x // tile_w)), []).append(r)
    order = sorted(tiles)
    step = int(cfg["tiles_per_read"])
    chunks = [[r for key in order[start:start + step] for r in tiles[key]]
              for start in range(0, len(order), step)]
    workers = max(1, int(getattr(model, "workers", 1) or 1))
    ready = queue.Queue(maxsize=max(int(cfg["read_ahead"]), workers))
    stop = threading.Event()

    def produce():
        try:
            for rows in chunks:
                if stop.is_set():
                    return
                ready.put((rows, _read(session, record, cells, rows, channels, crop_px)))
            ready.put(None)
        except BaseException as exc:  # handed to the consumer, raised there
            ready.put(exc)

    from collections import deque
    from concurrent.futures import ThreadPoolExecutor

    p_of = {}
    batch = int(cfg["batch_cells"])

    def work(rows, found):
        kept, x = tensors(found, [cells[r][0] for r in rows], marker, pixel_gate=pixel_gate,
                          levels=levels, co=co, anti=anti)
        del found
        p = [model(x[i:i + batch]) for i in range(0, len(kept), batch)]
        return len(rows), kept, (np.concatenate(p) if p else np.zeros(0, np.float32))

    reader = threading.Thread(target=produce, name="cellcall-read", daemon=True)
    reader.start()
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cellcall-model")
    in_hand = deque()
    done = 0

    def collect():
        nonlocal done
        n_rows, kept, p = in_hand.popleft().result()
        for cid, q in zip(kept, p.tolist()):
            p_of[cid] = q
        done += n_rows
        if progress:
            progress(done, len(run))

    try:
        while True:
            item = ready.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            in_hand.append(pool.submit(work, *item))
            del item
            while len(in_hand) > workers:
                collect()
            if cancelled and cancelled():
                raise InterruptedError("cancelled")
        while in_hand:
            collect()
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        while reader.is_alive():  # a reader blocked on a full queue lets go
            try:
                ready.get_nowait()
            except queue.Empty:
                reader.join(0.05)
    rows_out = [r for r in run if cells[r][0] in p_of]
    return {"rows": rows_out, "p": [p_of[cells[r][0]] for r in rows_out],
            "pixel_gate": float(pixel_gate), "pixel_gate_agreement": agreement,
            "n_calibration": len(means), "levels": levels, "plan": plan,
            "channels": sorted(channels), "crop_px": float(crop_px)}


# -- the rule -----------------------------------------------------------------------


def decide(rows, p, run_call, confident=None):
    """What the model changes: {promoted, demoted, uncertain} row arrays --
    the cells it confidently calls own where the gate says negative, not own
    where the gate says positive, and those it leans against the gate on
    without being confident (they keep the gate's call). `run_call` is the
    gate's calls on `rows` (`cell_exclusions` codes); a cell the gate cannot
    call (QC, no value) is never moved."""
    from plexora.agent.cell_exclusions import NEGATIVE, NOT_CALLED, POSITIVE

    c = CELLCALL["confident"] if confident is None else float(confident)
    rows = np.asarray(rows, dtype=np.int64)
    p = np.asarray(p, dtype=np.float64)
    at = np.asarray(run_call, dtype=np.int8)
    callable_ = at != NOT_CALLED
    own = callable_ & (p >= 1.0 - c)
    not_own = callable_ & (p <= c)
    leaning = callable_ & ~own & ~not_own & ((p > 0.5) != (at == POSITIVE))
    return {"promoted": rows[own & (at == NEGATIVE)], "demoted": rows[not_own & (at == POSITIVE)],
            "uncertain": rows[leaning], "confident": c}


def apply(gate_call, decided):
    """The gate's row-aligned calls with the model's confident ones written
    over them (a copy; `decided` from `decide`)."""
    from plexora.agent.cell_exclusions import NEGATIVE, POSITIVE

    call = np.array(gate_call, dtype=np.int8, copy=True)
    call[decided["promoted"]] = POSITIVE
    call[decided["demoted"]] = NEGATIVE
    return call


# -- cached runs ----------------------------------------------------------------------


def cache_key(ds, marker, low, high, selection_digest, model_id=None) -> str:
    """A stored run's key: the gate, data, QC, mask, model (`model_id`, else
    `cellcall_weights.identity()`), the cells read (`selection_digest`) and
    every `CELLCALL` entry that changes what the model says."""
    from plexora.plugins.gating.server.autogate import cellcall_weights, neighbourhood
    from plexora.plugins.gating.server.autogate import profile as profmod

    seg = ds.project.segmentation
    parts = [CELLCALL_VERSION, cellcall_weights.MODEL_VERSION,
             model_id or cellcall_weights.identity(),
             ds.name, marker, f"{float(low):.6g}", "" if high is None else f"{float(high):.6g}",
             profmod.fingerprint(ds), neighbourhood.qc_fingerprint(ds),
             repr(neighbourhood.mask_identity(ds.project)),
             str(getattr(seg, "source_key", "")), selection_digest,
             json.dumps({k: v for k, v in CELLCALL.items() if k not in RUNTIME_ONLY},
                        sort_keys=True)]
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:20]


def _digest(selection):
    blob = json.dumps({k: selection.get(k) for k in ("run", "calibration", "references",
                                                      "abundance")}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def _cache_dir(ds):
    from plexora import paths

    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(ds.name))
    return paths.agent_root() / "gating" / safe / "cellcall"


def _cache_path(ds, marker, key):
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(marker))
    return _cache_dir(ds) / f"{safe}_{key}.npz"


def load(ds, marker, low, high=None, *, model_id=None) -> dict | None:
    """The newest stored run for `marker` at exactly this gate (whatever the
    cells read) by the model in use (`model_id`, else the configured one),
    or None. A stored run is only ever read back for the gate, data, mask
    and model it was made with (its key)."""
    folder = _cache_dir(ds)
    if not folder.is_dir():
        return None
    try:
        from plexora.plugins.gating.server.autogate import cellcall_weights

        model_id = model_id or cellcall_weights.identity()
    except OSError:
        return None
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(marker))
    for path in sorted(folder.glob(f"{safe}_*.npz"), key=lambda p: p.stat().st_mtime,
                       reverse=True):
        try:
            with np.load(path, allow_pickle=False) as z:
                info = json.loads(str(z["info"]))
                if info.get("key") != cache_key(ds, marker, low, high, info.get("selection"),
                                                model_id):
                    continue
                out = {"rows": z["rows"].astype(np.int64), "p": z["p"].astype(np.float32),
                       "run_call": z["run_call"].astype(np.int8), "info": info}
                if "vf" in z.files:
                    out["vf"] = z["vf"].astype(np.float32)
                return out
        except Exception:
            continue
    return None


def _store(ds, marker, key, result, info):
    """Write a run; keep this marker's newest `keep_runs` (a session reads a
    marker near a provisional gate and again where a look moved it)."""
    folder = _cache_dir(ds)
    folder.mkdir(parents=True, exist_ok=True)
    path = _cache_path(ds, marker, key)
    tmp = path.with_suffix(".part.npz")
    extra = {"vf": np.asarray(result["vf"], np.float32)} if result.get("vf") is not None else {}
    np.savez_compressed(tmp, rows=np.asarray(result["rows"], np.int32),
                        p=np.asarray(result["p"], np.float32),
                        run_call=np.asarray(result["run_call"], np.int8),
                        info=np.array(json.dumps(info)), **extra)
    tmp.replace(path)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(marker))
    older = sorted((old for old in folder.glob(f"{safe}_*.npz") if old != path),
                   key=lambda q: q.stat().st_mtime, reverse=True)
    for old in older[max(0, int(CELLCALL["keep_runs"]) - 1):]:
        try:
            old.unlink()
        except OSError:
            pass


def run(session, ds, marker, low, high=None, *, gates=None, model=None, progress=None,
        cancelled=None, seed=0, refresh=False, band_halves=None, max_cells=None) -> dict:
    """The model's calls for `marker` at the gate `low` (cached on disk).

    {rows, p, run_call, vf, info} -- `rows` the table rows it read, in run
    order, `p` their p_own, `run_call` the gate's calls on them, `vf` their
    values in the fit space; `info` how (pixel gate, partners, how far from
    the gate it reached, seconds, the model). `band_halves` / `max_cells`
    narrow or widen the reach (`CELLCALL`'s otherwise). Raises
    `Unavailable` (with why) for a marker it cannot read."""
    import time

    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import cellcall_weights, tableops

    started = time.monotonic()
    pixel_um = cellmod.pixel_um(ds)
    selection = tableops.local_or_node(ds, "gating.autogate.cellcall_select", {
        "marker": marker, "low": float(low), "high": None if high is None else float(high),
        "gates": {k: (None if g is None else float(g)) for k, g in (gates or {}).items()},
        "pixel_um": pixel_um, "seed": int(seed), "band_halves": band_halves,
        "max_cells": max_cells})
    digest = _digest(selection)
    # The weights a run is keyed and recorded by: the model's own identity,
    # else the configured file's (a stand-in without one is keyed as it).
    model_id = getattr(model, "identity", None) if model is not None else None
    if model_id is None:
        model_id = cellcall_weights.identity()
    key = cache_key(ds, marker, low, high, digest, model_id)
    if not refresh:
        found = load(ds, marker, low, high, model_id=model_id)
        if found is not None and found["info"].get("key") == key:
            return found
    model = model or predictor()
    result = predict(session, ds, selection, model=model, progress=progress, cancelled=cancelled)
    position = {r: i for i, r in enumerate(selection["run"])}
    run_call = [selection["run_call"][position[r]] for r in result["rows"]]
    halves = [selection["run_halves"][position[r]] for r in result["rows"]]
    vf = [selection["run_vf"][position[r]] for r in result["rows"]]
    info = {"key": key, "selection": digest, "version": CELLCALL_VERSION,
            "model": model_id,
            "marker": marker, "low": float(low), "high": None if high is None else float(high),
            "n": selection["n"], "n_run": len(result["rows"]), "n_within": selection["n_within"],
            "capped": selection["capped"], "band_half": selection["band_half"],
            "gate_fit": selection["gate_fit"],
            "band_halves": selection["band_halves"], "max_cells": selection["max_cells"],
            "reach_halves": float(max(halves)) if halves else 0.0,
            "pixel_gate": result["pixel_gate"],
            "pixel_gate_agreement": result["pixel_gate_agreement"],
            "n_calibration": result["n_calibration"],
            "partners": [{k: p[k] for k in ("marker", "kind", "relation", "confidence")}
                         for p in result["plan"]],
            "crop_px": result["crop_px"], "seconds": round(time.monotonic() - started, 1)}
    out = {"rows": np.asarray(result["rows"], np.int64), "p": np.asarray(result["p"], np.float32),
           "run_call": np.asarray(run_call, np.int8), "vf": np.asarray(vf, np.float32),
           "info": info}
    _store(ds, marker, key, out, info)
    return out


def sweep(session, ds, jobs, *, model=None, progress=None, cancelled=None, on_result=None,
          refresh=False) -> dict:
    """Several markers' runs, one after another in the order given -- each
    one reading and running in parallel inside (`predict`) -- so the first
    marker is ready first. `jobs` [{marker, low, high?, gates?, band_halves?,
    max_cells?}]; `on_result(job, result_or_Unavailable)` as each finishes;
    `progress(index, n_jobs, done, n_cells)`. Returns {marker: result, or the
    `Unavailable` it raised}. A run already stored is read back."""
    model = model or predictor()
    out = {}
    for index, job in enumerate(jobs):
        if cancelled and cancelled():
            raise InterruptedError("cancelled")

        def step(done, n, index=index):
            if progress:
                progress(index, len(jobs), done, n)

        try:
            found = run(session, ds, job["marker"], job["low"], job.get("high"),
                        gates=job.get("gates"), model=model, progress=step,
                        cancelled=cancelled, refresh=refresh,
                        band_halves=job.get("band_halves"), max_cells=job.get("max_cells"))
        except Unavailable as exc:
            found = exc
        out[job["marker"]] = found
        if on_result:
            on_result(job, found)
    return out


def summary(result, confident=None) -> dict:
    """The counts a person reads: how many cells the model read, how many it
    moved up and down, how many positives it leaves uncertain -- and how."""
    info = result["info"]
    _out, counts = codes(info["n"], result, confident)
    return {"version": info.get("version"), "n_run": info["n_run"],
            "n_promoted": counts["own"], "n_demoted": counts["not_own"],
            "n_uncertain": counts["uncertain"],
            "confident": CELLCALL["confident"] if confident is None else float(confident),
            "reach_halves": info.get("reach_halves"), "capped": info.get("capped"),
            "pixel_gate": info.get("pixel_gate"), "partners": info.get("partners"),
            "seconds": info.get("seconds")}


# -- what is shown --------------------------------------------------------------------

#: The viewer's codes, one byte per table row (`setCellFlagMask`): a positive
#: the model leans against unconfidently, a positive it confidently reads as
#: not its own, a cell below the gate it confidently reads as its own.
CODE_UNCERTAIN, CODE_NOT_OWN, CODE_OWN = 1, 2, 3


def current(ds, marker):
    """(stored run or None, state, gate): the run at the marker's gate as it
    stands -- `ready`, `not_run` (none at this gate: never run, or the gate
    moved since), or `not_gated`."""
    from plexora.plugins.gating.server import model

    gate = model.get_gate(ds, marker)
    if gate is None or not gate.get("thresholded") or gate.get("low") is None:
        return None, "not_gated", gate
    found = load(ds, marker, gate["low"], gate["high"])
    return found, ("ready" if found is not None else "not_run"), gate


def codes(n, result, confident=None):
    """(uint8 per table row, counts): `CODE_*` where the model disagrees with
    the gate -- uncertain only over positives, as the viewer draws it."""
    from plexora.agent.cell_exclusions import POSITIVE

    out = np.zeros(int(n), dtype=np.uint8)
    found = decide(result["rows"], result["p"], result["run_call"], confident)
    at = dict(zip(np.asarray(result["rows"]).tolist(), np.asarray(result["run_call"]).tolist()))
    uncertain = np.array([r for r in found["uncertain"].tolist() if at.get(r) == POSITIVE],
                         dtype=np.int64)
    out[uncertain] = CODE_UNCERTAIN
    out[found["demoted"]] = CODE_NOT_OWN
    out[found["promoted"]] = CODE_OWN
    return out, {"own": int(found["promoted"].size), "not_own": int(found["demoted"].size),
                 "uncertain": int(uncertain.size)}
