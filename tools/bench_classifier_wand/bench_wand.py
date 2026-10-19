"""The classifier wand's recipes against the user's own labelled cells.

The labelled sets are the gating label galleries (2026-10-09 near-gate sets on
four images, the 2026-10-10 far set), kept out of git under
docs/internal/bench/runs/ -- or wherever PLEXORA_BENCH_RUNS points. Run from
the repository root, with PLEXORA_DATA_PATH (and HOME) pointing at a COPY of
the projects' data root, never the live install: the pixel features are cached
in each project's store.

    PYTHONPATH=. python tools/bench_classifier_wand/bench_wand.py PROJECT OUT.json [ARM,...]
    python tools/bench_classifier_wand/score_wand.py OUT1.json ... [--per-marker]

Results of 2026-10-19: docs/internal/bench/CLASSIFIER_WAND_2026-10-19.md.
"""
import json
import os
import sys
import time
from dataclasses import replace

import numpy as np

RUNS = os.environ.get("PLEXORA_BENCH_RUNS") or os.path.join(
    os.path.dirname(__file__), "..", "..", "docs", "internal", "bench", "runs")
SETS = {
    "lsp11385_2": [("near", "2026-10-09-labels"), ("far", "2026-10-10-labels-far")],
    "imc-immucan-example": [("near", "2026-10-09-labels-imc")],
    "codex-hubmap-lymphnode": [("near", "2026-10-09-labels-codex-ln")],
}
SEEDS = (0, 1, 2)


def load_labels(project):
    """{marker: {cell_id: (label 1/0, set name)}}; unsure and inconsistent repeats dropped."""
    out = {}
    for set_name, folder in SETS[project]:
        key = json.load(open(os.path.join(RUNS, folder, "key.json")))["tiles"]
        labels = json.load(open(os.path.join(RUNS, folder, "labels.json")))["labels"]
        seen = {}
        for tile, entry in labels.items():
            if entry.get("label") not in ("own", "glow", "none") or tile not in key:
                continue
            k = key[tile]
            y = 1 if entry["label"] == "own" else 0
            seen.setdefault((k["marker"], int(k["cell_id"])), []).append((y, k.get("table_call")))
        for (marker, cell), ys in seen.items():
            if len({y for y, _ in ys}) != 1:
                continue
            out.setdefault(marker, {})[cell] = (ys[0][0], set_name, ys[0][1])
    return out


def main():
    project, out_path = sys.argv[1], sys.argv[2]
    from tests.agent_fixtures import local_handles
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.classifier import autopick, features, learner, runs
    from plexora.plugins.gating.server.classifier.geometry import cells

    R = autopick.Recipe
    # The arms of 2026-10-19 that the shipped Recipe can still express (the
    # brightness-band and no-glow-rule controls were dropped from it).
    arms = {
        "first": autopick.FIRST,
        "more": R(count=40, agree=False, glow=False, hard_share=0.0),
        "agree": R(count=40, agree=True, glow=True, hard_share=0.0),
        "hard": autopick.RECIPE,
        "hard_noagree": R(count=40, agree=False, glow=False, hard_share=1 / 3),
        "hard_half": R(count=40, agree=True, glow=True, hard_share=0.5),
        "hard20": R(count=20, agree=True, glow=True, hard_share=1 / 3),
        "hard80": R(count=80, agree=True, glow=True, hard_share=1 / 3),
    }
    if len(sys.argv) > 3:
        arms = {k: v for k, v in arms.items() if k in sys.argv[3].split(",")}
    ds = local_handles(project)
    geometry = cells(ds)
    ids = geometry.ids
    order = np.argsort(ids, kind="stable")
    labels = load_labels(project)
    results = {"project": project, "markers": {}}
    for marker in sorted(labels):
        if marker not in ds.table.markers:
            print(project, marker, "not a marker; skipped", flush=True)
            continue
        t0 = time.time()
        matrix, names, info = features.feature_matrix(ds, marker)
        t_feat = time.time() - t0
        raw = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
        skip, _fp, _mode = runs.qc_state(ds, marker)
        eligible = np.isfinite(raw)
        if skip is not None and skip.shape[0] == eligible.shape[0]:
            eligible &= ~skip
        lab = labels[marker]
        lab_ids = np.asarray(list(lab), dtype=np.int64)
        at = np.clip(np.searchsorted(ids[order], lab_ids), 0, ids.size - 1)
        hit = ids[order][at] == lab_ids
        lab_rows = order[at[hit]]
        lab_ids = lab_ids[hit]
        y = np.asarray([lab[int(c)][0] for c in lab_ids])
        which = np.asarray([lab[int(c)][1] for c in lab_ids])
        keep = eligible[lab_rows]
        lab_rows, y, which, lab_ids = lab_rows[keep], y[keep], which[keep], lab_ids[keep]
        gate = (model.gmm_for(ds, marker) or {}).get("gate")
        entry = {"n_labels": int(y.size), "pixels": "pixels" in info["groups"],
                 "pixel_reason": info.get("pixel_reason"), "features_s": round(t_feat, 1),
                 "gmm_gate": gate, "y": y.tolist(), "set": which.tolist(),
                 "gate_call": (raw[lab_rows] > gate).astype(int).tolist() if gate is not None else None,
                 "key_call": [1 if lab[int(c)][2] == "pos" else 0 for c in lab_ids],
                 "n_eligible": int(eligible.sum()), "arms": {}}
        called = np.flatnonzero(eligible)
        for arm, recipe in arms.items():
            per_seed = []
            calls_by_seed = []
            for seed in SEEDS:
                t1 = time.time()
                try:
                    pos, neg, pinfo = autopick.pick(ds, marker, exclude=lab_ids.tolist(), seed=seed,
                                                    recipe=recipe, matrix=matrix, names=names)
                except Exception as exc:  # refused
                    per_seed.append({"error": str(exc)})
                    continue
                ex = np.asarray(pos + neg, dtype=np.int64)
                at2 = np.searchsorted(ids[order], ex)
                rows = order[at2]
                ylab = np.concatenate([np.ones(len(pos), int), np.zeros(len(neg), int)])
                fitted = learner.fit(matrix[rows], ylab, n_given=0)
                score, agreement = fitted.scores(matrix[called])
                call = (score >= 0.5)
                unc = learner.uncertain(agreement)
                full_call = np.zeros(raw.shape[0], dtype=np.int8)
                full_call[called] = call
                full_unc = np.zeros(raw.shape[0], dtype=bool)
                full_unc[called] = unc
                full_score = np.full(raw.shape[0], np.nan, np.float32)
                full_score[called] = score
                calls_by_seed.append(full_call[called])
                per_seed.append({
                    "pred": full_call[lab_rows].astype(int).tolist(),
                    "score": [round(float(v), 4) for v in full_score[lab_rows]],
                    "uncertain": full_unc[lab_rows].astype(int).tolist(),
                    "amber_share": float(unc.mean()),
                    "pos_share": float(call.mean()),
                    "model": fitted.kind, "cv": fitted.cv_balanced_accuracy,
                    "n_pos": len(pos), "n_neg": len(neg),
                    "hard": pinfo.get("hard_negatives"), "how": pinfo.get("how"),
                    "glow_test": pinfo.get("glow_test"),
                    "cand_pos": pinfo.get("candidates_positive"),
                    "s": round(time.time() - t1, 2)})
            stab = None
            if len(calls_by_seed) > 1:
                pairs = [(a, b) for i, a in enumerate(calls_by_seed) for b in calls_by_seed[i + 1:]]
                stab = float(np.mean([np.mean(a == b) for a, b in pairs]))
            entry["arms"][arm] = {"seeds": per_seed, "stability": stab}
        results["markers"][marker] = entry
        print(project, marker, "labels", y.size, "features", round(t_feat, 1), "s", flush=True)
        json.dump(results, open(out_path, "w"))
    json.dump(results, open(out_path, "w"))
    print("done", project, flush=True)


if __name__ == "__main__":
    main()
