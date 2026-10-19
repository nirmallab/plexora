"""Simulated review of uncertain cells (the panel's Review uncertain cells).

    PYTHONPATH=. python tools/bench_classifier_wand/bench_review.py PROJECT OUT.json
    python tools/bench_classifier_wand/score_review.py OUT1.json ...

Per marker and seed: the wand's picks (RECIPE) train a base model. The user's
labelled cells are split in two halves; from half A the model asks about k
cells -- the least agreed-on ("uncertain"), or k at random (control) -- and
their labels are added (weight 1, as a person's). Scored on half B, then the
halves swap.
"""
import json
import sys

import numpy as np

sys.path.insert(0, __import__("os").path.dirname(__file__))
from bench_wand import SEEDS, load_labels  # noqa: E402

KS = (4, 8)


def main():
    project, out_path = sys.argv[1], sys.argv[2]
    from tests.agent_fixtures import local_handles
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.classifier import autopick, features, learner, runs
    from plexora.plugins.gating.server.classifier.geometry import cells

    ds = local_handles(project)
    ids = cells(ds).ids
    order = np.argsort(ids, kind="stable")
    labels = load_labels(project)
    out = {"project": project, "rows": []}
    for marker in sorted(labels):
        if marker not in ds.table.markers:
            continue
        matrix, names, info = features.feature_matrix(ds, marker)
        raw = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
        skip, _fp, _mode = runs.qc_state(ds, marker)
        eligible = np.isfinite(raw)
        if skip is not None and skip.shape[0] == eligible.shape[0]:
            eligible &= ~skip
        lab = {c: v for c, v in labels[marker].items() if v[1] == "near"}
        lab_ids = np.asarray(list(lab), dtype=np.int64)
        at = np.clip(np.searchsorted(ids[order], lab_ids), 0, ids.size - 1)
        hit = ids[order][at] == lab_ids
        lab_rows, lab_ids = order[at[hit]], lab_ids[hit]
        keep = eligible[lab_rows]
        lab_rows, lab_ids = lab_rows[keep], lab_ids[keep]
        y = np.asarray([lab[int(c)][0] for c in lab_ids])
        gate = (model.gmm_for(ds, marker) or {}).get("gate")
        for seed in SEEDS:
            pos, neg, _ = autopick.pick(ds, marker, exclude=lab_ids.tolist(), seed=seed,
                                        matrix=matrix, names=names)
            ex = np.asarray(pos + neg, dtype=np.int64)
            ex_rows = order[np.searchsorted(ids[order], ex)]
            ex_y = np.concatenate([np.ones(len(pos), int), np.zeros(len(neg), int)])
            ex_w = np.full(ex_y.size, 0.5)
            base = learner.fit(matrix[ex_rows], ex_y, weights=ex_w, n_given=0)
            score, agreement = base.scores(matrix[lab_rows])
            rng = np.random.default_rng(100 + seed)
            perm = rng.permutation(y.size)
            halves = [perm[: y.size // 2], perm[y.size // 2:]]
            for fold in (0, 1):
                A, Bh = halves[fold], halves[1 - fold]
                row = {"marker": marker, "seed": seed, "fold": fold, "nB": int(Bh.size),
                       "yB": y[Bh].tolist(),
                       "gate": (raw[lab_rows[Bh]] > gate).astype(int).tolist() if gate is not None else None,
                       "base": (score[Bh] >= 0.5).astype(int).tolist()}
                # Least agreed-on first; ties by closeness of the score to 0.5.
                unc_order = A[np.lexsort((np.abs(score[A] - 0.5), agreement[A]))]
                for k in KS:
                    for how, chosen in (("uncertain", unc_order[:k]),
                                        ("random", rng.permutation(A)[:k])):
                        rows = np.concatenate([ex_rows, lab_rows[chosen]])
                        yy = np.concatenate([ex_y, y[chosen]])
                        ww = np.concatenate([ex_w, np.ones(chosen.size)])
                        if len(set(yy.tolist())) < 2:
                            continue
                        fitted = learner.fit(matrix[rows], yy, weights=ww, n_given=int(chosen.size))
                        sB, _aB = fitted.scores(matrix[lab_rows[Bh]])
                        row[f"{how}{k}"] = (sB >= 0.5).astype(int).tolist()
                        row[f"{how}{k}_pos_asked"] = int(y[chosen].sum())
                out["rows"].append(row)
        print(project, marker, flush=True)
        json.dump(out, open(out_path, "w"))
    print("done", project, flush=True)


if __name__ == "__main__":
    main()
