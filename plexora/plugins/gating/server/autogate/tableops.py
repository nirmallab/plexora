"""Automatic gating's column work, runnable where the table is.

Every function here reads whole marker columns, so for a project whose table
is on a data node it has to run on the node -- sending the columns to the
primary would be sending the table. Each is a registered table operation (JSON
in, JSON out) with a local fast path: on this machine `local_or_node` calls
the function directly with the caller's own handle set, so the agent session's
cache (the sorted column, the fit, the neighbour grid) is reused across calls
instead of being rebuilt for every operation.

Imported by `plugins/gating/server/tableops.py`, which a node imports, so the
node registers these too.
"""

from __future__ import annotations

import math

import numpy as np

from plexora.api import table_operation


def jsonable(value):
    """numpy scalars and arrays to plain Python, NaN/inf to None."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [jsonable(v) for v in value.tolist()]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def _profile(ds, payload):
    from plexora.plugins.gating.server.autogate import profile

    return profile.profile_marker(
        ds, payload["marker"], seed=int(payload.get("seed") or 0),
        n_boot=int(payload.get("n_boot", 5)),
        with_cell_qc=bool(payload.get("with_cell_qc", True)),
        compartment=payload.get("compartment"))


def _profile_all(ds, payload):
    from plexora.plugins.gating.server.autogate import profile

    markers = payload.get("markers") or list(ds.table.markers)
    out = {}
    for marker in markers:
        if marker not in ds.table.markers:
            continue
        out[marker] = profile.profile_marker(ds, marker, seed=int(payload.get("seed") or 0),
                                             n_boot=int(payload.get("n_boot", 5)))
    return {"profiles": out}


def _sample(ds, payload):
    from plexora.plugins.gating.server.autogate import sampler

    return sampler.stratified_cells(
        ds, payload["marker"], float(payload["low"]),
        None if payload.get("high") is None else float(payload["high"]),
        n_per_stratum=int(payload.get("n_per_stratum", 6)),
        include_inconsistent=int(payload.get("include_inconsistent", 6)),
        seed=int(payload.get("seed") or 0), strata=payload.get("strata"),
        within=payload.get("within"), spatial=payload.get("spatial"),
        partners=payload.get("partners"))


def _delta(ds, payload):
    from plexora.plugins.gating.server.autogate import sampler

    return sampler.delta_cells(
        ds, payload["marker"], payload["candidates"],
        None if payload.get("high") is None else float(payload["high"]),
        n_per_interval=int(payload.get("n_per_interval", 8)),
        n_regression=int(payload.get("n_regression", 8)),
        seed=int(payload.get("seed") or 0), within=payload.get("within"),
        spatial=payload.get("spatial"))


def _quadrants(ds, payload):
    from plexora.plugins.gating.server.autogate import sampler

    return sampler.quadrant_cells(ds, payload["a"], float(payload["gate_a"]), payload["b"],
                                  float(payload["gate_b"]),
                                  n_per_quadrant=int(payload.get("n_per_quadrant", 4)),
                                  seed=int(payload.get("seed") or 0))


def _bivariate(ds, payload):
    from plexora.plugins.gating.server.autogate import bivariate

    return bivariate.bivariate_numbers(
        ds, payload["a"], float(payload["gate_a"]), payload["b"], float(payload["gate_b"]),
        relation=payload.get("relation") or "independent",
        with_grid=bool(payload.get("with_grid", True)), seed=int(payload.get("seed") or 0))


def _candidates(ds, payload):
    from plexora.plugins.gating.server.autogate import candidates

    return candidates.candidate_thresholds(
        ds, payload["marker"], current_low=float(payload["current_low"]),
        direction=payload["direction"],
        high=None if payload.get("high") is None else float(payload["high"]),
        k=int(payload.get("k", 3)), gmm_gate=payload.get("gmm_gate"),
        controls=payload.get("controls") or ())


def _control(ds, payload):
    from plexora.plugins.gating.server.autogate import bivariate

    return {"control": bivariate.negative_control(
        ds, payload["a"], float(payload["gate_a"]), payload["b"], float(payload["gate_b"]),
        relation=payload.get("relation") or "independent")}


def _within(ds, payload):
    from plexora.plugins.gating.server.autogate import bivariate

    return bivariate.within_partner(
        ds, payload["marker"], payload["partner"], float(payload["partner_gate"]),
        current=None if payload.get("current") is None else float(payload["current"]),
        seed=int(payload.get("seed") or 0))


def _regression(ds, payload):
    from plexora.plugins.gating.server.autogate import regression

    return regression.numeric_checks(ds, payload["marker"], float(payload["final"]),
                                     float(payload["gmm"]), payload.get("prior"),
                                     payload.get("partners") or [],
                                     spatial=payload.get("spatial"))


def _gates_at(ds, payload):
    """Positive counts for many thresholds of one marker (one sort)."""
    from plexora.plugins.gating.server.autogate import profile

    col = profile.column(ds, payload["marker"])
    lows = [float(v) for v in payload["lows"]]
    high = payload.get("high")
    counts = col.n_positive_many(lows, None if high is None else float(high))
    return {"lows": lows, "n_positive": counts.tolist(), "n_finite": col.n_finite}


def _spatial_map(ds, payload):
    """The background map a payload carries, decoded once per fingerprint."""
    from plexora.plugins.gating.server.autogate import spatial

    encoded = payload.get("map")
    if not encoded:
        return None
    return ds.cached(("autogate.spatial.decoded", encoded.get("fingerprint")),
                     lambda: spatial.decode_map(encoded))


def _spatial(ds, payload):
    """The feature layer's summary at one gate (`spatial.spatial_summary`)."""
    from plexora.plugins.gating.server.autogate import spatial

    low = float(payload["low"])
    high = None if payload.get("high") is None else float(payload["high"])
    bgmap = _spatial_map(ds, payload)
    key = ("autogate.spatial.summary", payload["marker"], round(low, 9), high,
           payload.get("mode"), (bgmap or {}).get("fingerprint"), payload.get("pixel_um"),
           spatial.SPATIAL_VERSION)
    return ds.cached(key, lambda: spatial.spatial_summary(
        ds, payload["marker"], low, high, bgmap=bgmap, mode=payload.get("mode"),
        pixel_um=payload.get("pixel_um"), seed=int(payload.get("seed") or 0)))


def _spatial_cells(ds, payload):
    """Per-cell features for a bounded list of cell ids ({cell_id: record})."""
    from plexora.agent.limits import MAX_IDS
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import spatial

    bgmap = _spatial_map(ds, payload)
    feats = spatial.per_cell(ds, payload["marker"], bgmap=bgmap,
                             pixel_um=payload.get("pixel_um"))
    gate = spatial.at_gate(ds, payload["marker"], feats, float(payload["low"]),
                           payload.get("high"), mode=payload.get("mode") or "C", bgmap=bgmap)
    c = cellmod.cells(ds)
    wanted = [int(i) for i in (payload.get("cell_ids") or [])][:MAX_IDS]
    rows = np.flatnonzero(np.isin(c.ids, wanted))
    return {"cells": {str(int(c.ids[r])): spatial.cell_record(feats, gate, int(r))
                      for r in rows}}


def _contrast(ds, payload):
    from plexora.plugins.gating.server.autogate import sampler

    return sampler.contrast_cells(
        ds, payload["marker"], float(payload["low"]),
        None if payload.get("high") is None else float(payload["high"]),
        side=payload.get("side") or "above", n_per_row=int(payload.get("n_per_row", 6)),
        seed=int(payload.get("seed") or 0), within=payload.get("within"),
        spatial=payload.get("spatial"), partners=payload.get("partners"))


def _purity(ds, payload):
    """`bivariate.purity_excess` for one partner: how impure a gate's
    positives are against it, and how impure its brightest ones already are
    (the partner's own imperfection)."""
    from plexora.plugins.gating.server.autogate import bivariate

    found = bivariate.purity_excess(ds, payload["marker"], float(payload["low"]),
                                    payload["partner"], float(payload["partner_gate"]),
                                    relation=payload.get("relation") or "independent")
    return {"purity": found}


def _uncertain(ds, payload):
    """The uncertain positives at a gate (`calls.uncertain_mask`): their
    count, and their row indices when `rows` is asked for."""
    from plexora.plugins.gating.server.autogate import calls

    spatial = {k: payload.get(k) for k in ("mode", "map", "pixel_um")}
    mask = calls.uncertain_mask(ds, payload["marker"], float(payload["low"]),
                                payload.get("high"), spatial=spatial,
                                confirmed_ids=payload.get("confirmed") or ())
    if mask is None:
        return {"n_uncertain": 0, "rows": [] if payload.get("rows") else None}
    out = {"n_uncertain": int(mask.sum()), "version": calls.UNCERTAIN_VERSION}
    if payload.get("rows"):
        out["rows"] = np.flatnonzero(mask).tolist()
    return out


OPERATIONS = {
    "gating.autogate.profile": _profile,
    "gating.autogate.profile_all": _profile_all,
    "gating.autogate.sample": _sample,
    "gating.autogate.delta": _delta,
    "gating.autogate.quadrants": _quadrants,
    "gating.autogate.bivariate": _bivariate,
    "gating.autogate.candidates": _candidates,
    "gating.autogate.control": _control,
    "gating.autogate.within": _within,
    "gating.autogate.regression": _regression,
    "gating.autogate.gates_at": _gates_at,
    "gating.autogate.spatial": _spatial,
    "gating.autogate.spatial_cells": _spatial_cells,
    "gating.autogate.contrast": _contrast,
    "gating.autogate.purity": _purity,
    "gating.autogate.uncertain": _uncertain,
}


def _register(name, fn):
    @table_operation(name)
    def operation(dataset, payload):
        from plexora.agent import cell_exclusions

        # The QC record the caller resolved (a node has no QC store to ask).
        with cell_exclusions.from_payload(payload):
            return jsonable(fn(dataset, dict(payload or {})))

    return operation


for _name, _fn in OPERATIONS.items():
    _register(_name, _fn)


def local_or_node(ds, name, payload):
    """Run one operation: in-process on this machine's table (reusing the
    handle set's cache), on the node that holds it otherwise."""
    from plexora.agent import cell_exclusions

    fn = OPERATIONS[name]
    if ds.table.is_local:
        return jsonable(fn(ds, dict(payload)))
    return ds.table.run(name, cell_exclusions.attach(ds, payload))
