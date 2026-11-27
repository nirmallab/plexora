"""The numbers a step's plots are drawn from, read from the working file.

Scimap Pro writes results into the working AnnData; this module reads back
only what a plot needs -- one obs column, one obsm key, one uns table, and the
expression matrix in row chunks for group means -- with h5py, never loading
the whole file. Each summary is computed once per run and cached in the
notebook (store.put_result), so reopening a notebook draws at once.

Two things are kept honest here rather than in the page:

* **absent stays absent.** A distance to a cell type an image does not have
  is NaN in Scimap Pro's table and `null` here, drawn as a dash, never 0;
* **the unit is stated.** Every summary carries its experimental unit and the
  number of replicates, so "12 clusters across 184,112 cells" is never read as
  a cohort claim from one image.
"""

from __future__ import annotations

import math

import numpy as np

#: The most points sent to the page for a scatter. Above it a stratified sample
#: is drawn and the page says so ("Showing 200,000 of 2,031,446").
SCATTER_MAX = 200_000
CHUNK_ROWS = 200_000


def clean(value):
    """JSON-safe: NaN and inf become None, numpy becomes Python."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, np.ndarray):
        return clean(value.tolist())
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _open(path):
    import h5py

    return h5py.File(str(path), "r")


def _read(group, key):
    from anndata.io import read_elem

    return read_elem(group[key])


def natural(values):
    def key(v):
        text = str(v)
        try:
            return (0, float(text), "")
        except ValueError:
            return (1, 0.0, text.lower())
    return sorted(values, key=key)


def obs_column(path, name):
    """One obs column as strings (categorical) or floats."""
    import pandas as pd

    with _open(path) as f:
        if name not in f["obs"]:
            raise KeyError(name)
        values = _read(f["obs"], name)
    if isinstance(values, pd.Categorical):
        return np.asarray(values.astype(str))
    return np.asarray(values)


def _var_names(f):
    import pandas as pd

    index_key = f["var"].attrs.get("_index", "_index")
    names = _read(f["var"], index_key)
    return [str(v) for v in (names.tolist() if isinstance(names, (pd.Index, np.ndarray)) else names)]


def scimappro_data(fn, path, **kwargs):
    """The table one of Scimap Pro's `pl` functions draws (`returnData=True`):
    every number a summary shows comes from Scimap Pro, not from Plexora."""
    from plexora.plugins.scimap.server.adapter import scimappro

    sm = scimappro()
    out = getattr(sm.pl, fn)(str(path), returnData=True, show=False, verbose=False, progress=False,
                             **{k: v for k, v in kwargs.items() if v is not None})
    return out[0] if isinstance(out, tuple) else out


def counts_by_image(path, column, work):
    """Cells of each category of `column` in each image, by `pl.barplot`."""
    table = scimappro_data("barplot", path, xAxis=work.get("image_column") or "imageid", yAxis=column,
                           method="absolute")
    table.columns = [str(c) for c in table.columns]
    table.index = [str(i) for i in table.index]
    return table


def group_means(path, column, markers=None):
    """Mean expression of each group, and each marker scaled to 0..1 across the
    groups: `pl.heatmap`'s own numbers. `(means, scaled)` as DataFrames."""
    common = dict(groupBy=column, subsetMarkers=list(markers) if markers else None,
                  clusterRows=False, clusterColumns=False)
    means = scimappro_data("heatmap", path, standardScale=None, **common)
    scaled = scimappro_data("heatmap", path, standardScale="column", **common)
    for frame in (means, scaled):
        frame.index = [str(i) for i in frame.index]
        frame.columns = [str(c) for c in frame.columns]
    return means, scaled


def _codes(values):
    cats = natural(np.unique(values.astype(str)))
    index = {c: i for i, c in enumerate(cats)}
    return np.asarray([index[v] for v in values.astype(str)], dtype=np.int64), cats


def unit_line(unit, n_images):
    unit = unit or {}
    name = unit.get("unit") or "imageid"
    n = unit.get("nReplicates")
    if n is None:
        n = n_images
    word = {"imageid": "image", "edge": "neighbor pair", "cell": "cell"}.get(str(name), str(name))
    plural = "" if n == 1 else "s"
    count = f"{n:,}" if isinstance(n, int) else str(n)
    if word == "neighbor pair":
        # Scimap Pro tests pairs of neighbors, which are not independent
        # samples: with this many, nearly any difference is "significant".
        images = "image" if n_images == 1 else "images"
        return {"unit": word, "n": n, "pairs": True,
                "text": f"Experimental unit: neighbor pair · {count} pairs in {n_images} {images}. "
                        "Pairs are not independent samples, so with this many nearly any difference "
                        "has a small p; compare images to generalize"}
    return {"unit": word, "n": n, "text": f"Experimental unit: {word} · {count} replicate{plural}"}


# -- per tool -------------------------------------------------------------


def labels_summary(path, column, work, *, heat=True, markers=None):
    """Sizes, mean expression and highest markers of each group of `column`,
    from `pl.barplot` and `pl.heatmap`."""
    table = counts_by_image(path, column, work)
    cats = natural(table.columns.tolist())
    pooled = table[cats].sum(axis=0)
    total = int(pooled.sum())
    sizes = [{"id": c, "count": int(pooled[c]), "pct": (100.0 * pooled[c] / total) if total else 0.0}
             for c in cats]
    out = {"column": column, "n_cells": total, "groups": sizes}
    if heat:
        means, scaled = group_means(path, column, markers)
        means, scaled = means.reindex(cats), scaled.reindex(cats)
        out["heat"] = {"markers": list(means.columns), "scaled": np.round(scaled.to_numpy(float), 3).tolist(),
                       "means": np.round(means.to_numpy(float), 4).tolist()}
        out["top_markers"] = [[m for m in row.sort_values(ascending=False).index[:3] if row[m] > 0.5]
                              for _, row in scaled.iterrows()]
    if total:
        biggest = max(sizes, key=lambda s: s["count"])
        smallest = min(sizes, key=lambda s: s["count"])
        out["largest_pct"], out["smallest_pct"] = biggest["pct"], smallest["pct"]
    return out


def scatter(path, key, color=None, limit=SCATTER_MAX):
    """Embedding coordinates, sampled to `limit`, with codes for `color`."""
    with _open(path) as f:
        if key not in f["obsm"]:
            raise KeyError(key)
        coords = np.asarray(f["obsm"][key][:, :2], dtype=float)
    n = coords.shape[0]
    idx = np.arange(n)
    sampled = n > limit
    if sampled:
        rng = np.random.default_rng(0)
        idx = np.sort(rng.choice(n, limit, replace=False))
    pts = coords[idx]
    out = {"n_total": int(n), "n_shown": int(len(idx)), "sampled": bool(sampled),
           "x": np.round(pts[:, 0], 3).tolist(), "y": np.round(pts[:, 1], 3).tolist(),
           "extent": [float(np.nanmin(coords[:, 0])), float(np.nanmax(coords[:, 0])),
                      float(np.nanmin(coords[:, 1])), float(np.nanmax(coords[:, 1]))]}
    if color:
        out.update(color_codes(path, color, idx))
    return out


def color_codes(path, column, idx=None):
    values = obs_column(path, column)
    if values.dtype.kind in "fiu" and len(np.unique(values)) > 60:
        sel = values if idx is None else values[idx]
        return {"color": column, "color_kind": "number", "values": np.round(sel.astype(float), 4).tolist(),
                "range": [float(np.nanmin(values)), float(np.nanmax(values))]}
    codes, cats = _codes(values)
    counts = np.bincount(codes, minlength=len(cats))
    sel = codes if idx is None else codes[idx]
    return {"color": column, "color_kind": "category", "codes": sel.tolist(), "categories": cats,
            "pcts": (100.0 * counts / max(1, counts.sum())).tolist()}


def lasso(path, key, polygon, color=None):
    """Every cell of the embedding inside `polygon` (not only the shown ones):
    `{n, total, top: [{id, pct}]}` and the cell positions, for a selection."""
    from matplotlib.path import Path as MplPath

    with _open(path) as f:
        coords = np.asarray(f["obsm"][key][:, :2], dtype=float)
    inside = MplPath(np.asarray(polygon, dtype=float)).contains_points(coords)
    out = {"n": int(inside.sum()), "total": int(len(coords)), "top": []}
    if color and out["n"]:
        values = obs_column(path, color)[inside].astype(str)
        uniq, counts = np.unique(values, return_counts=True)
        order = np.argsort(-counts)[:3]
        out["top"] = [{"id": str(uniq[i]), "pct": 100.0 * counts[i] / out["n"]} for i in order]
    return out, inside


def composition(path, column, work):
    """Cells of each kind per image (`pl.barplot`, counts and shares)."""
    table = counts_by_image(path, column, work)
    cats = natural(table.columns.tolist())
    rows = natural(table.index.tolist())
    table = table.loc[rows, cats]
    pct = scimappro_data("barplot", path, xAxis=work.get("image_column") or "imageid", yAxis=column,
                         method="percent")
    pct.columns = [str(c) for c in pct.columns]
    pct.index = [str(i) for i in pct.index]
    # Scimap Pro's shares are fractions of each image.
    pct = pct.reindex(index=rows, columns=cats).fillna(0.0) * 100.0
    pooled = table.sum(axis=0)
    return {"column": column, "categories": cats, "images": rows,
            "counts": table.values.tolist(), "pcts": pct.to_numpy(float).tolist(),
            "pooled_counts": pooled.values.tolist(),
            "pooled_pcts": np.round(100.0 * pooled.values / max(1, pooled.sum()), 3).tolist(),
            "n_cells": int(pooled.sum())}


def _uns_frame(path, key):
    with _open(path) as f:
        if key not in f["uns"]:
            raise KeyError(key)
        return _read(f["uns"], key)


def _uns_dict(path, key):
    try:
        value = _uns_frame(path, key)
    except KeyError:
        return {}
    return value if isinstance(value, dict) else {}


def distance(path, label, source, work):
    frame = _uns_frame(path, f"{label}_samples")
    frame = frame.drop(columns=[c for c in frame.columns if "::" not in str(c)], errors="ignore")
    pairs = [str(c).split("::", 1) for c in frame.columns]
    cats = natural(sorted({a for a, _ in pairs} | {b for _, b in pairs}))
    images = [str(i) for i in frame.index.tolist()]
    per = {}
    for image, row in zip(images, frame.itertuples(index=False)):
        matrix = [[None] * len(cats) for _ in cats]
        for (a, b), v in zip(pairs, row):
            matrix[cats.index(a)][cats.index(b)] = None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
        per[image] = matrix
    params = _uns_dict(path, f"{label}_params")
    pixel = (work.get("pixel_size") or {})
    one = list(pixel.values())[0] if len(set(pixel.values())) == 1 and pixel else None
    return {"categories": cats, "images": images, "matrices": per,
            "statistic": params.get("summary", "median"), "distance_unit": "px", "pixel_size": one,
            "experimental_unit": {"unit": params.get("unitOfReplication"),
                                  "nReplicates": params.get("nReplicates")}}


def interaction(path, label, source, work):
    frame = _uns_frame(path, label)
    cats = natural(sorted(set(frame["phenotype"].astype(str)) | set(frame["neighbour_phenotype"].astype(str))))
    images = natural(sorted(set(frame["image"].astype(str))))
    per = {}
    for image in images:
        sub = frame[frame["image"].astype(str) == image]
        score = [[None] * len(cats) for _ in cats]
        sig = [[None] * len(cats) for _ in cats]
        for row in sub.itertuples(index=False):
            i, j = cats.index(str(row.phenotype)), cats.index(str(row.neighbour_phenotype))
            score[i][j] = clean(float(row.logRatio))
            sig[i][j] = clean(float(row.pval_adj))
        per[image] = {"score": score, "p": sig}
    params = _uns_dict(path, f"{label}_params")
    return {"categories": cats, "images": images, "matrices": per, "statistic": "log ratio",
            "null_model": f"{params.get('nPerms', params.get('permutation', '?'))} label permutations within each image",
            "experimental_unit": {"unit": params.get("unitOfReplication"),
                                  "nReplicates": params.get("nReplicates")}}


def neighborhoods(path, label, source, work):
    """Sizes of each neighborhood, and what it holds: the share of each cell
    kind among its cells (`pl.barplot`)."""
    out = labels_summary(path, label, work, heat=False)
    if not source:
        return out
    table = scimappro_data("barplot", path, xAxis=label, yAxis=source, method="percent")
    table.columns = [str(c) for c in table.columns]
    table.index = [str(i) for i in table.index]
    kinds = natural(table.columns.tolist())
    cats = [g["id"] for g in out["groups"]]
    table = table.reindex(index=cats, columns=kinds).fillna(0.0)  # fractions
    out["composition"] = {"kinds": kinds, "fractions": np.round(table.to_numpy(float), 4).tolist()}
    return out


def _num(value, digits=3):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else round(v, digits)


def markers_summary(path, label, source, work, top=5):
    """Find markers: each group's top markers by score, and the full table."""
    frame = _uns_frame(path, label)
    frame = frame.assign(group=frame["group"].astype(str), feature=frame["feature"].astype(str))
    groups = natural(sorted(set(frame["group"])))
    tops, rows = {}, []
    for g in groups:
        sub = frame[frame["group"] == g].sort_values("score", ascending=False)
        tops[g] = [f for f, sc in zip(sub["feature"], sub["score"]) if float(sc) > 0][:top]
        for rank, row in enumerate(sub.itertuples(index=False), start=1):
            rows.append([g, rank, row.feature, _num(row.score, 2), _num(getattr(row, "logFC", None), 2),
                         _num(100 * float(getattr(row, "pctGroup", float("nan"))), 1)])
    return {"headline": {"strong": f"Top markers of {len(groups)} groups",
                         "rest": f" in {source}. Read them before naming the groups; they describe the "
                                 "groups, they do not test them."},
            "tops": tops, "column": source,
            "table": {"columns": [{"key": "group", "label": "Group"}, {"key": "rank", "label": "Rank", "num": True},
                                  {"key": "marker", "label": "Marker"}, {"key": "score", "label": "Score", "num": True},
                                  {"key": "fc", "label": "log fold change", "num": True},
                                  {"key": "pct", "label": "% of group", "num": True}],
                      "rows": rows}}


def quality_summary(path, label, work):
    """Check quality: how many cells were flagged, and why, per image."""
    table = counts_by_image(path, label, work)
    kinds = [k for k in table.columns if k != "keep"]
    rows = []
    for image, row in table.iterrows():
        n = int(row.sum())
        bad = int(sum(row[k] for k in kinds))
        rows.append([str(image), n, bad] + [int(row[k]) for k in kinds] + [_num(100 * bad / max(n, 1), 1)])
    total = int(table.values.sum())
    flagged = int(sum(table[k].sum() for k in kinds))
    why = ", ".join(f"{int(table[k].sum()):,} {k}" for k in kinds) if kinds else "none"
    return {"headline": {"strong": f"{flagged:,} of {total:,} cells flagged",
                         "rest": f" ({100 * flagged / max(total, 1):.1f}%): {why}. Nothing was deleted; the "
                                 f"flags are in {label}."},
            "flagged": flagged, "n_cells": total, "column": label,
            "table": {"columns": [{"key": "image", "label": "Image"}, {"key": "n", "label": "Cells", "num": True},
                                  {"key": "flagged", "label": "Flagged", "num": True}]
                      + [{"key": k, "label": k.capitalize(), "num": True} for k in kinds]
                      + [{"key": "pct", "label": "% flagged", "num": True}],
                      "rows": rows}}


def compare_summary(path, label, work):
    """Compare groups: one row per cell type, its difference and interval."""
    frame = _uns_frame(path, label)
    params = _uns_dict(path, f"{label}_params")
    groups = [str(g) for g in (params.get("groups") if params.get("groups") is not None else [])]
    frame = frame.reset_index()
    feature = "feature" if "feature" in frame.columns else frame.columns[0]
    means = [c for c in frame.columns if str(c).startswith("mean_")]
    rows = []
    for row in frame.to_dict("records"):
        rows.append([str(row.get(feature)), _num(row.get("effect"), 4), _num(row.get("ci_low"), 4),
                     _num(row.get("ci_high"), 4)] + [_num(row.get(m), 4) for m in means]
                    + [_num(row.get("pval"), 4), _num(row.get("pval_adj"), 4), str(row.get("test") or "")])
    adj = [r[-2] for r in rows if r[-2] is not None]
    hits = sum(1 for v in adj if v < 0.05)
    n = params.get("nReplicates")
    return {"headline": {"strong": f"{hits} of {len(rows)} cell types differ",
                         "rest": f" between {' and '.join(groups) if groups else 'the groups'} at an adjusted "
                                 f"p below 0.05, with each of {n} samples counted once."},
            "groups": groups,
            "experimental_unit": {"unit": params.get("unitOfReplication"), "nReplicates": n},
            "table": {"columns": [{"key": "type", "label": "Cell type"},
                                  {"key": "effect", "label": "Difference", "num": True},
                                  {"key": "lo", "label": "95% low", "num": True},
                                  {"key": "hi", "label": "95% high", "num": True}]
                      + [{"key": m, "label": "Mean " + str(m)[5:], "num": True} for m in means]
                      + [{"key": "p", "label": "p", "num": True}, {"key": "padj", "label": "Adjusted p", "num": True},
                         {"key": "test", "label": "Test"}],
                      "rows": rows}}


#: Most rows a ranked-gene table sends; the rest stay in the working file.
SVG_ROWS = 2000
#: The adjusted p below which Scimap Pro's plot marks a gene as patterned (its own default).
SVG_P = 0.05


def svg_summary(path, label, work):
    """Spatially variable genes: Scimap Pro's ranked table (`pl.spatiallyVariableFeatures`, `returnData`), most
    organised first in each sample, with what it marks as patterned (adjusted p below `pVal`)."""
    n = len(work.get("markers") or ()) or SVG_ROWS
    frame = scimappro_data("spatiallyVariableFeatures", path, label=label, nFeatures=n, pVal=SVG_P)
    word = {"I": "Moran's I", "C": "Geary's C"}.get(str(frame["statistic"].iloc[0]) if len(frame) else "", "Statistic")
    hits = frame[frame["pval_adj"] < SVG_P]
    tops = [str(f) for f in frame.sort_values("rank")["feature"].astype(str).drop_duplicates().head(10)]
    rows = [[str(r["feature"]), str(r["image"]), int(r["rank"]), _num(r["value"], 4), _num(r["zscore"], 3),
             _num(r["pval_adj"], 4)] for r in frame.head(SVG_ROWS).to_dict("records")]
    more = f" Showing the first {SVG_ROWS:,} rows." if len(frame) > SVG_ROWS else ""
    return {"headline": {"strong": f"{len(hits):,} of {len(frame):,} gene–sample pairs patterned",
                         "rest": f" at an adjusted p below {SVG_P:g}, by {word}; most organised first: "
                                 f"{', '.join(tops[:5])}.{more}"},
            "tops": tops, "column": None,
            "table": {"columns": [{"key": "gene", "label": "Gene"}, {"key": "image", "label": "Sample"},
                                  {"key": "rank", "label": "Rank", "num": True},
                                  {"key": "stat", "label": word, "num": True},
                                  {"key": "z", "label": "z-score", "num": True},
                                  {"key": "padj", "label": "Adjusted p", "num": True}],
                      "rows": rows}}


def pca_summary(eff, work):
    """PCA & neighbours: what was run, as it was called."""
    pca = next((s.get("kwargs") or {} for s in eff.get("before") or () if s.get("key") == "pp.pca"), {})
    genes = "the most variable genes" if pca.get("useHighlyVariable") else "every gene"
    k = (eff.get("kwargs") or {}).get("nNeighbors")
    return {"headline": {"strong": f"{pca.get('nComps')} principal components of {genes}",
                         "rest": f", from the layer {pca.get('layer') or 'X'}, and a graph of each cell's {k} "
                                 "nearest neighbours in them. Clustering and UMAP read the graph."}}


def normalize_summary(eff, work):
    """Normalize: what was written, and where."""
    scaled = next((s.get("kwargs") or {} for s in eff.get("before") or () if s.get("key") == "pp.normalizeTotal"),
                  {})
    target = float(scaled.get("targetSum") or 10000)
    source = (eff.get("inputs") or {}).get("layer") or "X"
    layer = (eff.get("kwargs") or {}).get("targetLayer") or eff.get("label")
    from plexora.plugins.scimap.server import lexicon

    unit = lexicon.words(work.get("data_mode"))["unit"]
    return {"headline": {"strong": f"Counts scaled to {target:,.0f} per {unit} and logged",
                         "rest": f", from {source} into the layer {layer}. Clustering, markers and the other "
                                 "steps read it from here on."}}


def summarize(tool_id, eff, path, work) -> dict:
    """The summary of one completed step, ready to cache and send."""
    label = eff.get("label")
    source = (eff.get("inputs") or {}).get("source")
    n_images = max(1, len(work.get("images") or []))
    kwargs = eff.get("kwargs") or {}
    if tool_id in ("cluster", "celltypes", "name_groups"):
        body = labels_summary(path, label, work)
        if tool_id == "name_groups" and source:
            body["mapping"] = kwargs.get("rename")
    elif tool_id == "markers":
        body = markers_summary(path, label, source, work)
    elif tool_id == "compare":
        body = compare_summary(path, label, work)
    elif tool_id == "quality":
        body = quality_summary(path, label, work)
    elif tool_id == "svg":
        body = svg_summary(path, label, work)
    elif tool_id == "normalize":
        body = normalize_summary(eff, work)
    elif tool_id == "pca":
        body = pca_summary(eff, work)
    elif tool_id == "umap":
        # The points are in Scimap Pro's figure (figures.py); the summary
        # carries only what the page prints.
        with _open(path) as f:
            n = int(f["obsm"][label].shape[0])
        body = {"n_total": n, "color": (eff.get("display") or {}).get("color")}
    elif tool_id == "composition":
        body = composition(path, source, work)
    elif tool_id == "expression":
        body = labels_summary(path, source, work, markers=kwargs.get("markers"))
    elif tool_id == "distance":
        body = distance(path, label, source, work)
    elif tool_id == "interaction":
        body = interaction(path, label, source, work)
    elif tool_id == "neighborhoods":
        body = neighborhoods(path, label, source, work)
    else:
        body = {}
    unit = body.pop("experimental_unit", None) or eff.get("experimental_unit")
    body["unit"] = unit_line(unit, n_images)
    return clean(body)
