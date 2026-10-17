"""Partner bands: where a marker's background ends, read off its partners' own cells.

Every other use of a partner (`scoring`'s curves, the negative control, the
purity check) runs through the partner's GATE, so a partner still under
review -- or never gated -- says nothing, and one wrong gate misleads every
marker that leans on it. Bands need no gate. Each partner's own mixture
splits its cells into sure positives (above its positive population's mean)
and sure negatives (below its background's mean plus `dark_sd` of its sds;
without a mixture, its body's top and bottom quantiles), and the marker's
values among those cells say:

- `floor:<P>` -- the 99th percentile of the marker among cells that cannot
  carry it: the sure negatives of a partner it is a subset of or co-expressed
  with (CD3 among CD45-dark cells), the sure positives of an exclusive one
  (CD3 among PanCK-bright cells). A gate under it calls more than one in a
  hundred known negatives positive.
- `anchor:<P>` -- the valley of the marker among a superset partner's sure
  positives (CD8 among CD3-bright cells splits CD8+ from CD4+), when the two
  populations there are separated by at least `anchor_min_d`
  (`bivariate.within_partner`'s refit, seeded).
- `sens:<P>` -- the 5th percentile of the marker among cells that should all
  carry it: a co-expressed partner's sure positives, or a child's (CD8-bright
  cells on CD3's axis). A gate above it drops true positives.

The consensus is the median of each kind over every partner that gave one --
several co-expressed and several exclusive partners vote, so no single
partner's spill or odd stain decides -- with `partners_disagree` when the
floors spread more than `disagree_sd` background sds, and
`floor_above_anchor` when the floor sits over the anchor (the anchor is the
target then; the window check gives no verdict).

Evidence, never a gate: the scoring may lead with the anchor and lift a
proposal a little to the floor (`scoring`), the lattice gets the points, a
look whose gate sits far from them gets a second look at the cells between
(`transitions._look`), and a gate outside the window fails a whole-image
check -- the looks still place it.

Pure over (data, marker, partners): a table operation (`gating.autogate.
bands`) that needs no session state, so it runs on a data node; one that does
not have it is answered by the same function on the primary
(`tableops.local_or_node_or_fallback`).
"""

from __future__ import annotations

import hashlib
import json

import numpy as np

#: Bumped whenever a band's numbers change meaning; part of the memo key with
#: `SessionOptions.partners = bands`.
VERSION = "1"

#: [cal] `min_cells`: a side (sure positives or sure negatives) with fewer
#: cells gives nothing; `dark_sd`: sure negatives sit under the partner's
#: background mean plus this many of its sds; `floor_q` / `sens_q`: the
#: quantiles a floor and a sensitivity bound are read at; `anchor_min_d`:
#: the separation a within-partner valley needs; `fallback_*_q`: the body
#: quantiles of a partner without a mixture; `disagree_sd`: floors spread
#: wider than this many background sds disagree; `floor_lift_sd`: a proposal
#: this close under the floor is lifted to it (`scoring`); `partner_window_sd`:
#: a gate this far outside [floor, anchor] fails the window check
#: (`regression`) and a T1 gate goes to a look; `partner_look_sd`: a gate this
#: far from the anchor (or under the floor) gets the partner look.
PARAMS = {"min_cells": 200, "dark_sd": 1.0, "floor_q": 0.99, "sens_q": 0.05,
          "anchor_min_d": 1.5, "fallback_sure_q": 0.99, "fallback_dark_q": 0.30,
          "disagree_sd": 2.0, "floor_lift_sd": 2.0, "partner_window_sd": 2.0,
          "partner_look_sd": 2.0}

#: What each relation contributes, by (relation, direction).
CONTRIBUTES = {("subset", "forward"): ("floor", "anchor"),
               ("coexpressed", "forward"): ("floor", "sens"),
               ("coexpressed", "reverse"): ("floor", "sens"),
               ("exclusive", "forward"): ("floor",),
               ("exclusive", "reverse"): ("floor",),
               ("subset", "reverse"): ("sens",)}

#: The kinds of band, in the order a consensus is read.
KINDS = ("floor", "anchor", "sens")


def _r(value):
    return None if value is None else float(f"{float(value):.6f}")


def partner_bands(ds, marker, partners, *, gate=None, seed=0, min_cells=None) -> dict:
    """{version, marker, fit_space, n_cells, gate_fit, background, partners,
    consensus, flags, notes} for `marker` against `partners` ([{marker,
    relation, direction, derived?, confidence?}]). Values in the marker's
    fit space (`*_fit`) and raw (`low`)."""
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import profile as profmod
    from plexora.plugins.gating.server.autogate import scoring

    least = int(min_cells or PARAMS["min_cells"])
    out = {"version": VERSION, "marker": marker, "partners": [], "consensus": {},
           "flags": [], "notes": []}
    col = profmod.column(ds, marker)
    body = col.body if col.body.size else col.fit
    out["fit_space"] = "log1p" if col.to_log else "raw"
    out["n_cells"] = int(col.n_finite)
    out["gate_fit"] = None if gate is None else _r(col.to_fit(float(gate)))
    if body.size < least:
        out["notes"].append("too few cells for partner bands")
        return out
    bg = scoring.background(body)
    if bg is None:
        out["notes"].append("a constant column")
        return out
    sd = float(bg["sd"])
    out["background"] = {"mode_fit": _r(bg["mode"]), "sd_fit": _r(sd)}
    x = col.to_fit(cellmod.values(ds, marker))
    finite = np.isfinite(x)
    markers = set(ds.table.markers)
    for partner in partners or ():
        row = _partner_row(ds, marker, partner, x, finite, markers, col, least, seed,
                           cellmod, profmod)
        out["partners"].append(row)
    out["consensus"], out["flags"] = consensus(out["partners"], sd,
                                               lambda v: float(col.from_fit(v)))
    if not out["consensus"]:
        out["notes"].append("no partner gave a band")
    return out


def _partner_row(ds, marker, partner, x, finite, markers, col, least, seed, cellmod,
                 profmod):
    name = partner.get("marker")
    relation = partner.get("relation")
    direction = partner.get("direction") or "forward"
    row = {"marker": name, "relation": relation, "direction": direction}
    for key in ("derived", "confidence"):
        if partner.get(key) is not None:
            row[key] = partner[key]
    kinds = CONTRIBUTES.get((relation, direction))
    if not kinds or name == marker or name not in markers:
        row["skipped"] = "no band for this relation" if not kinds else "not a marker here"
        return row
    try:
        col_p = profmod.column(ds, name)
        y = col_p.to_fit(cellmod.values(ds, name))
    except Exception as exc:  # a partner that cannot be read gives nothing
        row["skipped"] = f"unreadable: {str(exc)[:120]}"
        return row
    sure_cut, dark_cut, rule = _cuts(ds, name, col_p, profmod)
    if sure_cut is None:
        row["skipped"] = "a constant partner"
        return row
    both = finite & np.isfinite(y)
    sure = both & (y > sure_cut)
    dark = both & (y < dark_cut)
    row["sure"] = {"rule": rule, "cut_fit": _r(sure_cut), "n": int(sure.sum())}
    row["dark"] = {"rule": rule, "cut_fit": _r(dark_cut), "n": int(dark.sum())}
    if sure.sum() < least and dark.sum() < least:
        row["skipped"] = "too few sure cells either side"
        return row
    if "floor" in kinds:
        # Cells that cannot carry the marker: an exclusive partner's
        # positives, a superset's (or co-expressed partner's) negatives.
        cells = sure if relation == "exclusive" else dark
        if cells.sum() >= least:
            value = float(np.quantile(x[cells], PARAMS["floor_q"]))
            row["floor"] = {"fit": _r(value), "low": float(col.from_fit(value)),
                            "n": int(cells.sum())}
    if "anchor" in kinds and sure.sum() >= least:
        anchor = _anchor(np.sort(x[sure]), seed, profmod)
        if anchor is not None:
            row["anchor"] = {"fit": _r(anchor[0]), "low": float(col.from_fit(anchor[0])),
                             "d": _r(anchor[1]), "n": int(sure.sum())}
    if "sens" in kinds and sure.sum() >= least:
        value = float(np.quantile(x[sure], PARAMS["sens_q"]))
        row["sens"] = {"fit": _r(value), "low": float(col.from_fit(value)),
                       "n": int(sure.sum())}
    return row


def _cuts(ds, name, col_p, profmod):
    """(sure cut, dark cut, rule) in the partner's fit space: its mixture's
    positive mean and background mean + `dark_sd` sds, else its body's
    quantiles. (None, None, None) for a constant partner."""
    try:
        fit = profmod.fit_for(ds, name)
    except Exception:
        fit = None
    if fit is not None and bool(fit.get("fitted_in_log", col_p.to_log)) == bool(col_p.to_log):
        p = profmod.pools(fit)
        sure_cut = float(p["mu_pos"])
        dark_cut = float(p["mu_bg"] + PARAMS["dark_sd"] * p["sd_bg"])
        if sure_cut > dark_cut:
            return sure_cut, dark_cut, "mixture"
    body = col_p.body if col_p.body.size else col_p.fit
    if not body.size:
        return None, None, None
    sure_cut, dark_cut = (float(v) for v in np.quantile(
        body, [PARAMS["fallback_sure_q"], PARAMS["fallback_dark_q"]]))
    if not sure_cut > dark_cut:
        return None, None, None
    return sure_cut, dark_cut, "quantile"


def _anchor(values, seed, profmod):
    """(valley, separation) of the marker among a superset partner's sure
    positives -- a two-component refit there -- or None when the two
    populations are not separated by `anchor_min_d`."""
    fitted = profmod._fit_light(values, 2, seed=seed)
    if fitted is None:
        return None
    means, sds, _weights = fitted
    d = float(abs(means[1] - means[0]) / np.sqrt((sds[0] ** 2 + sds[1] ** 2) / 2.0))
    if not np.isfinite(d) or d < PARAMS["anchor_min_d"]:
        return None
    gate = profmod.pools(fitted)["gate"]
    return float(gate), d


def consensus(rows, sd_fit, to_raw=float):
    """({floor, anchor, sens}: {fit, low, from, spread_fit}, flags) -- the
    median of each kind over the partners that gave one (`to_raw` maps a
    fit-space value to the table's)."""
    out, flags = {}, []
    for kind in KINDS:
        found = sorted(((r[kind]["fit"], r[kind]["low"], r["marker"]) for r in rows or ()
                        if isinstance(r.get(kind), dict) and r[kind].get("fit") is not None),
                       key=lambda item: (item[0], item[2]))
        if not found:
            continue
        fits = np.array([f for f, _low, _m in found])
        median = float(np.median(fits))
        out[kind] = {"fit": _r(median), "low": float(to_raw(median)),
                     "from": sorted(m for _f, _low, m in found),
                     "spread_fit": _r(float(fits.max() - fits.min()))}
    floor, anchor = out.get("floor"), out.get("anchor")
    if floor and len(floor["from"]) >= 2 and sd_fit \
            and floor["spread_fit"] > PARAMS["disagree_sd"] * float(sd_fit):
        flags.append("partners_disagree")
    if floor and anchor and floor["fit"] > anchor["fit"]:
        flags.append("floor_above_anchor")
    return out, flags


def candidates(bands) -> list:
    """[(id, family, fit value)]: the scoring candidates bands give --
    `panchor` / `pfloor` / `psens` (the consensus) and `anchor:<P>` /
    `floor:<P>` / `psens:<P>` (each partner's)."""
    if not bands or not bands.get("consensus"):
        return []
    out = []
    names = {"anchor": ("panchor", "partner_anchor"), "floor": ("pfloor", "partner_floor"),
             "sens": ("psens", "sensitivity")}
    for kind in KINDS:
        entry = bands["consensus"].get(kind)
        if entry:
            out.append((names[kind][0], names[kind][1], float(entry["fit"])))
    for row in bands.get("partners") or ():
        for kind in KINDS:
            entry = row.get(kind)
            if isinstance(entry, dict) and entry.get("fit") is not None:
                prefix = "psens" if kind == "sens" else kind
                out.append((f"{prefix}:{row['marker']}", names[kind][1], float(entry["fit"])))
    return out


def window(bands, k=None):
    """(low, high) in the fit space: [floor - k sd, anchor + k sd], either
    side None when its band is missing; (None, None) when the floor is over
    the anchor (no verdict)."""
    if not bands or not bands.get("consensus"):
        return None, None
    k = PARAMS["partner_window_sd"] if k is None else k
    sd = float(((bands.get("background") or {}).get("sd_fit")) or 0.0)
    if "floor_above_anchor" in (bands.get("flags") or ()):
        return None, None
    floor = (bands["consensus"].get("floor") or {}).get("fit")
    anchor = (bands["consensus"].get("anchor") or {}).get("fit")
    return (None if floor is None else float(floor) - k * sd,
            None if anchor is None else float(anchor) + k * sd)


def fingerprint(bands) -> str:
    """A short hash of what the bands say (the scoring's cache key)."""
    if not bands:
        return ""
    blob = json.dumps({"version": bands.get("version"), "consensus": bands.get("consensus"),
                       "partners": [{k: r.get(k) for k in ("marker", "floor", "anchor", "sens")}
                                    for r in bands.get("partners") or ()]},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def compact(bands) -> dict:
    """What a unit keeps of a bands result: the consensus, the flags, and
    each partner's three numbers."""
    if not bands:
        return {}
    keep = ("version", "marker", "fit_space", "n_cells", "gate_fit", "background",
            "consensus", "flags", "notes", "skipped", "computed")
    out = {k: bands[k] for k in keep if k in bands}
    out["partners"] = [{k: r[k] for k in ("marker", "relation", "direction", "floor",
                                          "anchor", "sens", "skipped") if k in r}
                       for r in bands.get("partners") or ()]
    out["fingerprint"] = fingerprint(bands)
    return out


def line(bands) -> str:
    """The bands in one line, for the report and the status."""
    if not bands or not bands.get("consensus"):
        return ""
    parts = []
    for kind, word in (("floor", "floor"), ("anchor", "anchor"), ("sens", "sensitivity")):
        entry = bands["consensus"].get(kind)
        if entry:
            parts.append(f"{word} {entry['low']:.4g} ({', '.join(entry['from'])})")
    flags = bands.get("flags") or ()
    if flags:
        parts.append(", ".join(f.replace("_", " ") for f in flags))
    return "partner bands: " + "; ".join(parts)
