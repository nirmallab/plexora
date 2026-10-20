"""The gate-plus-classifier rule against the user's own labelled cells.

For each marker and gate arm (`human`: the gate the labelled cells were called
at; `gmm`: Thresholding's Auto) and seed, the wand's picks outside the bands
either side of the gate train a classifier (weight 0.5 each, as runs.py does),
which scores every eligible cell; the scores of the labelled cells and the
override counts of every margin of the grid over ALL eligible cells are
recorded. Same label sets and data-root rules as bench_wand.py: run from the
repository root against a COPY of the data root, never the live install.

    PLEXORA_DATA_PATH=<copy>/data HOME=<copy>/home PLEXORA_BENCH_RUNS=<runs> \\
    PYTHONPATH=.:../spatialagents/src python tools/bench_classifier_wand/bench_rule.py PROJECT OUT.json
    python tools/bench_classifier_wand/score_rule.py OUT1.json ... [--arm gmm] [--tables]

Results of 2026-10-19: docs/internal/bench/CLASSIFIER_RULE_2026-10-19.md.
"""
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from bench_wand import SEEDS, load_labels  # noqa: E402

C_ADDS = (None, 0.02, 0.05, 0.1)
C_CLEANS = (None, 0.05, 0.1, 0.2, 0.3)
MIN_AGREEMENT = 0.8
CONSISTENT = 0.95  # a stored gate agrees with the table calls at this share


def best_cut(values, calls):
    """(cut, errors, separable): the cut between `values` that misplaces the
    fewest table calls (value > cut is pos); ties take the middle of the run."""
    order = np.argsort(values, kind="stable")
    v, c = values[order], calls[order]
    n = v.size
    # errors for a cut after position i (cells 0..i are called neg)
    pos_below = np.concatenate([[0], np.cumsum(c == 1)])
    neg_above = (c == 0).sum() - np.concatenate([[0], np.cumsum(c == 0)])
    err = pos_below + neg_above
    best = err.min()
    idx = np.flatnonzero(err == best)
    i = int(idx[len(idx) // 2])
    lo = v[i - 1] if i > 0 else v[0]
    hi = v[i] if i < n else v[-1]
    return float((lo + hi) / 2.0), int(best), bool(best == 0)


def human_gate(ds, marker, values, calls):
    """The gate the labelled cells were called at, and how it was found."""
    from plexora.plugins.gating.server import model
    ok = np.isfinite(values)
    values, calls = values[ok], calls[ok]
    if values.size == 0 or len(set(calls.tolist())) < 2:
        return None, {"how": "no table calls on both sides"}
    cut, err, sep = best_cut(values, calls)
    info = {"how": "separable midpoint" if sep else "best separating value",
            "derived": cut, "table_call_errors": err, "n": int(values.size)}
    rec = model.get_gate(ds, marker) or {}
    info["stored"] = rec.get("low") if rec.get("thresholded") else None
    info["stored_thresholded"] = bool(rec.get("thresholded"))
    if rec.get("thresholded"):
        agree = float(np.mean((values > rec["low"]).astype(int) == calls))
        info["stored_agreement"] = agree
        info["stored_consistent"] = agree >= CONSISTENT
        if agree >= CONSISTENT:
            info["how"] = "stored gate"
            return float(rec["low"]), info
    return cut, info


def main():
    project, out_path = sys.argv[1], sys.argv[2]
    from tests.agent_fixtures import local_handles
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.classifier import autopick, features, learner, rules, runs, signal
    from plexora.plugins.gating.server.classifier.geometry import cells

    ds = local_handles(project)
    ids = cells(ds).ids
    order = np.argsort(ids, kind="stable")
    labels = load_labels(project)
    results = {"project": project, "markers": {}}
    for marker in sorted(labels):
        if marker not in ds.table.markers:
            print(project, marker, "not a marker; skipped", flush=True)
            continue
        t0 = time.time()
        matrix, names, info = features.feature_matrix(ds, marker)
        raw = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
        skip, _fp, _mode = runs.qc_state(ds, marker)
        eligible = np.isfinite(raw)
        if skip is not None and skip.shape[0] == eligible.shape[0]:
            eligible &= ~skip
        lab = labels[marker]
        lab_ids = np.asarray(list(lab), dtype=np.int64)
        at = np.clip(np.searchsorted(ids[order], lab_ids), 0, ids.size - 1)
        hit = ids[order][at] == lab_ids
        lab_rows, lab_ids = order[at[hit]], lab_ids[hit]
        word = {}
        for cell, v in lab.items():
            word[int(cell)] = v
        y = np.asarray([lab[int(c)][0] for c in lab_ids])
        which = [lab[int(c)][1] for c in lab_ids]
        tcall = np.asarray([1 if lab[int(c)][2] == "pos" else 0 for c in lab_ids])
        keep = eligible[lab_rows]
        lab_rows, lab_ids, y, tcall = lab_rows[keep], lab_ids[keep], y[keep], tcall[keep]
        which = [w for w, k in zip(which, keep) if k]
        lab_vals = raw[lab_rows]
        called = np.flatnonzero(eligible)
        d = signal.separation(ds, marker)
        gmm_gate = (model.gmm_for(ds, marker) or {}).get("gate")
        hg, hinfo = human_gate(ds, marker, lab_vals, tcall)
        entry = {"n_labels": int(y.size), "y": y.tolist(), "set": which, "value": lab_vals.tolist(),
                 "table_call": tcall.tolist(), "d": d, "human_info": hinfo,
                 "pixels": "pixels" in info["groups"], "n_eligible": int(eligible.sum()), "gates": {}}
        for arm, G in (("human", hg), ("gmm", gmm_gate)):
            if G is None:
                entry["gates"][arm] = {"error": "no gate"}
                continue
            G = float(G)
            band = signal.band(ds, marker, G, signal.BAND_HALVES)[0]
            above = called[raw[called] > G]
            contrast = signal.pixel_contrast(matrix, names, above)
            gate_calls = (raw[called] > G).astype(np.int8)
            g = {"gate": G, "contrast": contrast, "n_above": int(above.size),
                 "band": None if band is None else [band[0], band[2]],
                 "side": (lab_vals > G).astype(int).tolist(), "seeds": []}
            for seed in SEEDS:
                try:
                    pos, neg, pinfo = autopick.pick(
                        ds, marker, exclude=lab_ids.tolist(), seed=seed, matrix=matrix, names=names,
                        gate=G, exclude_range=None if band is None else (band[0], band[2]))
                except Exception as exc:  # refused
                    g["seeds"].append({"error": f"pick: {exc}"})
                    continue
                ex = np.asarray(pos + neg, dtype=np.int64)
                rows = order[np.searchsorted(ids[order], ex)]
                ylab = np.concatenate([np.ones(len(pos), int), np.zeros(len(neg), int)])
                w = np.full(ylab.shape, runs.AUTO_WEIGHT)
                fitted = learner.fit(matrix[rows], ylab, weights=w, n_given=0)
                score, agree = fitted.scores(matrix[called])
                amber = float(learner.uncertain(agree).mean())
                raw_calls = rules.apply(score, gate_calls, "score_0.5")
                full_s = np.full(raw.shape[0], np.nan, np.float32)
                full_a = np.full(raw.shape[0], np.nan, np.float32)
                full_s[called], full_a[called] = score, agree
                counts = {"raw": rules.compare(raw_calls, gate_calls, raw_calls)}
                for ca in C_ADDS:
                    for cc in C_CLEANS:
                        for ma in (None, MIN_AGREEMENT):
                            out = rules.apply(score, gate_calls, "gate_unless_confident", c_add=ca,
                                              c_clean=cc, agreement=agree, min_agreement=ma)
                            counts[f"{ca}|{cc}|{ma}"] = rules.compare(out, gate_calls, raw_calls)
                g["seeds"].append({
                    "score": [round(float(v), 4) for v in full_s[lab_rows]],
                    "agreement": [round(float(v), 4) for v in full_a[lab_rows]],
                    "amber_share": amber, "counts": counts, "model": fitted.kind,
                    "n_pos": len(pos), "n_neg": len(neg), "hard": pinfo.get("hard_negatives"),
                    "cv": fitted.cv_balanced_accuracy})
            entry["gates"][arm] = g
        results["markers"][marker] = entry
        print(project, marker, "labels", y.size, "human gate", hg, hinfo.get("how"), "gmm", gmm_gate,
              "d", d, round(time.time() - t0, 1), "s", flush=True)
        json.dump(results, open(out_path, "w"))
    json.dump(results, open(out_path, "w"))
    print("done", project, flush=True)


if __name__ == "__main__":
    main()
