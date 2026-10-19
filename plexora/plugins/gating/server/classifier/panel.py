"""What the panel and an agent ask of the classifier, answered once.

The routes and the capabilities are both thin wrappers over these, so a
click in the viewer and an agent's call change the same state and report it
in the same words.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.gating.server.classifier import (
    MIN_EXAMPLES, ClassifierRefused, calls, runs, store)

MODES = ("threshold", "classifier")

#: How far (image px, level 0) a click may land from a cell and still pick it
#: when the click is not on a labelled pixel.
PICK_RADIUS_PX = 8


def marker_of(ds, marker):
    if marker not in ds.table.markers:
        raise ClassifierRefused(f"{marker!r} is not a marker of this project")
    return marker


def positions(ds, ids):
    """[{cell_id, x, y}] for cell ids, in the table's coordinates; ids the
    table does not have are left out."""
    from plexora.plugins.gating.server.classifier.geometry import cells

    geometry = cells(ds)
    if not ids:
        return []
    order = np.argsort(geometry.ids, kind="stable")
    sorted_ids = geometry.ids[order]
    wanted = np.asarray(ids, dtype=np.int64)
    at = np.clip(np.searchsorted(sorted_ids, wanted), 0, max(sorted_ids.size - 1, 0))
    out = []
    for value, index in zip(wanted.tolist(), at.tolist()):
        if sorted_ids.size and sorted_ids[index] == value:
            row = order[index]
            x, y = float(geometry.xs[row]), float(geometry.ys[row])
            if np.isfinite(x) and np.isfinite(y):
                out.append({"cell_id": int(value), "x": x, "y": y})
    return out


def examples_payload(ds, marker):
    """The marker's examples with their places, and which the wand picked."""
    pos, neg = store.examples(ds.name, marker)
    origin = store.origins(ds.name, marker)

    def placed(ids):
        out = positions(ds, ids)
        for entry in out:
            entry["auto"] = origin.get(entry["cell_id"]) == "auto"
        return out

    return {"positive": placed(pos), "negative": placed(neg),
            "n_positive": len(pos), "n_negative": len(neg),
            "n_auto": sum(1 for o in origin.values() if o == "auto")}


def state(ds, marker=None) -> dict:
    """The mode, every marker's example counts and active source, and for
    `marker` its examples (with positions), run and stale reason."""
    document = store.read_state(ds.name)
    counts = store.counts(ds.name)
    sources = {m: ("classifier" if (e or {}).get("active") == "classifier"
                   and (e or {}).get("run") else "threshold")
               for m, e in document["markers"].items()}
    out = {"mode": document.get("mode") or "threshold", "min_examples": MIN_EXAMPLES,
           "counts": {m: {"positive": p, "negative": n} for m, (p, n) in counts.items()},
           "classified": [m for m, s in sources.items() if s == "classifier"],
           "running": runs.running(ds.name)}
    if marker is not None:
        marker_of(ds, marker)
        record = store.run_record(ds.name, marker)
        examples = examples_payload(ds, marker)
        out["marker"] = {
            "marker": marker,
            "source": sources.get(marker, "threshold"),
            "examples": examples,
            "run": calls.public_run(record),
            "stale": runs.stale_reason(ds, record) if record else None,
            "can_run": (examples["n_positive"] >= MIN_EXAMPLES
                        and examples["n_negative"] >= MIN_EXAMPLES),
            "has_baseline": bool((document["markers"].get(marker) or {}).get("baseline")),
        }
    return out


def set_mode(ds, mode) -> str:
    if mode not in MODES:
        raise ClassifierRefused(f"mode is one of {', '.join(MODES)}")
    with store.editing(ds.name) as document:
        document["mode"] = mode
    return mode


def cell_at(ds, x, y, radius=PICK_RADIUS_PX) -> dict:
    """The cell under image point (x, y), level 0: the mask's label there,
    else the nearest labelled pixel within `radius`, else (no mask) the
    nearest centroid within `radius`. {cell_id: None} when there is none."""
    from plexora.plugins.gating.server.classifier.geometry import cells

    geometry = cells(ds)
    known = geometry.ids[geometry.ids >= 0]
    x, y, r = float(x), float(y), max(1, int(radius))
    if ds.project.segmentation.available:
        from plexora.agent import render

        provider, _status, _reason, _locator = render._mask_for(ds.project)
        if provider is not None:
            cx, cy = int(np.floor(x)), int(np.floor(y))
            box = (cx - r, cy - r, cx + r + 1, cy + r + 1)
            labels = np.asarray(provider.read_region(0, box))
            hit = None
            if labels.shape == (2 * r + 1, 2 * r + 1):
                centre = int(labels[r, r])
                if centre:
                    hit, how = centre, "mask"
                else:
                    ys, xs = np.nonzero(labels)
                    if ys.size:
                        d2 = (ys - r) ** 2 + (xs - r) ** 2
                        best = int(np.argmin(d2))
                        if d2[best] <= r * r:
                            hit, how = int(labels[ys[best], xs[best]]), "mask_nearby"
            if hit is not None and np.isin(hit, known):
                return {"cell_id": hit, "method": how, **_centroid(ds, hit)}
            if hit is not None:
                return {"cell_id": None, "method": "mask",
                        "reason": f"mask label {hit} is not a cell of the table"}
            return {"cell_id": None, "method": "mask", "reason": "no cell there"}
    valid = geometry.valid
    if not valid.any():
        return {"cell_id": None, "method": "centroid", "reason": "the table has no positions"}
    tree = ds.cached(("classifier.centroid_tree",), lambda: _tree(geometry))
    distance, index = tree.query([x, y], k=1)
    if not np.isfinite(distance) or distance > r:
        return {"cell_id": None, "method": "centroid", "reason": "no cell there"}
    row = np.flatnonzero(valid)[int(index)]
    hit = int(geometry.ids[row])
    return {"cell_id": hit, "method": "centroid", **_centroid(ds, hit)}


def _tree(geometry):
    from scipy.spatial import cKDTree

    return cKDTree(np.column_stack([geometry.xs[geometry.valid], geometry.ys[geometry.valid]]))


def _centroid(ds, cell_id):
    found = positions(ds, [cell_id])
    return {"x": found[0]["x"], "y": found[0]["y"]} if found else {}


LABELS = {"positive": 1, "negative": 0, None: None, "": None, "remove": None}


def annotate(ds, marker, cell_id, label, *, toggle=True, origin="click") -> dict:
    """Label one cell. With `toggle`, the label it already has removes it
    (a second click undoes the first); the other label flips it."""
    marker_of(ds, marker)
    if label not in LABELS:
        raise ClassifierRefused("label is positive, negative or null")
    wanted = LABELS[label]
    if cell_id is None:
        raise ClassifierRefused("no cell there")
    cell_id = int(cell_id)
    if not positions(ds, [cell_id]):
        raise ClassifierRefused(f"cell {cell_id} is not in this project's table")
    pos, neg = store.examples(ds.name, marker)
    current = 1 if cell_id in pos else (0 if cell_id in neg else None)
    if toggle and wanted is not None and current == wanted:
        wanted = None
    before = store.set_label(ds.name, marker, cell_id, wanted, origin=origin)
    after = {1: "positive", 0: "negative", None: None}[wanted]
    return {"marker": marker, "cell_id": cell_id, "before": before, "after": after,
            "examples": examples_payload(ds, marker)}


def clear_examples(ds, markers=None) -> dict:
    if markers is not None:
        for marker in markers:
            marker_of(ds, marker)
    return {"cleared": store.clear_examples(ds.name, markers),
            "markers": None if markers is None else list(markers)}
