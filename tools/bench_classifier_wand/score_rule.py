"""Score bench_rule.py results.

    python tools/bench_classifier_wand/score_rule.py OUT1.json [OUT2.json ...] [--arm human|gmm] [--tables]

Prints, per image/set and pooled: the gate, the raw classifier and the grid of
gate_unless_confident margins -- agreement, balanced accuracy, recall of own
cells at or below the gate, specificity on glow/none cells above it, and the
precision of additions and removals, each averaged over the seeds (spread is
the sd of the per-seed pooled value). `--snr` adds terciles of the marker's
separation d and pixel contrast. `--markers` lists the per-marker signal.
Pure numpy; the rule is rules.apply, as the bench ran it.
"""
import json
import sys

import numpy as np

sys.path.insert(0, ".")
from plexora.plugins.gating.server.classifier import rules  # noqa: E402

C_ADDS = (None, 0.02, 0.05, 0.1)
C_CLEANS = (None, 0.05, 0.1, 0.2, 0.3)


EXTEND = "--extend" in sys.argv  # post-hoc margins beyond the registered grid


def arms_list():
    out = ["gate", "raw"]
    adds, cleans = C_ADDS, C_CLEANS
    if EXTEND:
        adds, cleans = (None, 0.1, 0.2, 0.3, 0.4, 0.5), (None, 0.3, 0.4, 0.5)
    for ma in (None, 0.8):
        for ca in adds:
            for cc in cleans:
                if ca is None and cc is None:
                    continue
                out.append(f"guc a={ca} c={cc} ag={ma}")
    return out


def predict(arm, score, agree, side):
    if arm == "gate":
        return side.copy()
    gc = side.astype(np.int8)
    if arm == "raw":
        return rules.apply(score, gc, "score_0.5")
    parts = arm.split()
    ca = None if parts[1][2:] == "None" else float(parts[1][2:])
    cc = None if parts[2][2:] == "None" else float(parts[2][2:])
    ma = None if parts[3][3:] == "None" else float(parts[3][3:])
    return rules.apply(score, gc, "gate_unless_confident", c_add=ca, c_clean=cc, agreement=agree, min_agreement=ma)


def stats(y, p, side):
    """Counts for one seed; sums across cells are added later."""
    y, p, side = np.asarray(y), np.asarray(p), np.asarray(side)
    ok = p >= 0
    y, p, side = y[ok], p[ok], side[ok]
    added = (p == 1) & (side == 0)
    removed = (p == 0) & (side == 1)
    return {"n": y.size, "ok": int((y == p).sum()),
            "tp": int(((y == 1) & (p == 1)).sum()), "n1": int((y == 1).sum()),
            "tn": int(((y == 0) & (p == 0)).sum()), "n0": int((y == 0).sum()),
            "own_below": int(((y == 1) & (side == 0)).sum()),
            "rescued": int(((y == 1) & (side == 0) & (p == 1)).sum()),
            "neg_above": int(((y == 0) & (side == 1)).sum()),
            "cleaned": int(((y == 0) & (side == 1) & (p == 0)).sum()),
            "added": int(added.sum()), "added_own": int((added & (y == 1)).sum()),
            "removed": int(removed.sum()), "removed_neg": int((removed & (y == 0)).sum())}


def combine(a, b):
    return {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)}


def metrics(s):
    def r(a, b):
        return a / b if b else float("nan")
    bal = np.nanmean([r(s["tp"], s["n1"]), r(s["tn"], s["n0"])])
    return {"n": s["n"], "agree": r(s["ok"], s["n"]), "bal": float(bal),
            "rescue": r(s["rescued"], s["own_below"]), "clean_spec": r(s["cleaned"], s["neg_above"]),
            "add_prec": r(s["added_own"], s["added"]), "n_add": s["added"],
            "rem_prec": r(s["removed_neg"], s["removed"]), "n_rem": s["removed"]}


def collect(paths, gate_arm):
    """records: one per (image, marker, seed, arm) with per-label stats and marker signal."""
    arms = arms_list()
    recs = []
    for path in paths:
        d = json.load(open(path))
        img = d["project"]
        for marker, e in d["markers"].items():
            g = e["gates"].get(gate_arm)
            if not g or "error" in g:
                continue
            y = np.asarray(e["y"])
            sets = np.asarray(e["set"])
            side = np.asarray(g["side"])
            for si, s in enumerate(g["seeds"]):
                if "error" in s:
                    recs.append({"image": img, "marker": marker, "seed": si, "error": s["error"]})
                    continue
                score = np.asarray(s["score"], dtype=np.float32)
                agree = np.asarray(s["agreement"], dtype=np.float32)
                for arm in arms:
                    p = predict(arm, score, agree, side)
                    for st in sorted(set(sets.tolist())):
                        m = sets == st
                        recs.append({"image": img, "set": st, "marker": marker, "seed": si, "arm": arm,
                                     "d": e["d"], "contrast": g["contrast"], "amber": s["amber_share"],
                                     "stats": stats(y[m], p[m], side[m])})
    return recs


def agg(recs, arm, **filt):
    """Seed-averaged metrics for the records of `arm` matching filt, pooled over markers."""
    sel = [r for r in recs if r.get("arm") == arm and all(r.get(k) == v for k, v in filt.items())]
    if not sel:
        return None
    seeds = sorted({r["seed"] for r in sel})
    per_seed = []
    for sd in seeds:
        tot = {}
        for r in sel:
            if r["seed"] == sd:
                tot = combine(tot, r["stats"])
        per_seed.append(metrics(tot))
    out = {k: float(np.nanmean([m[k] for m in per_seed])) for k in per_seed[0]}
    out["bal_sd"] = float(np.nanstd([m["bal"] for m in per_seed]))
    return out


def fmt(m):
    if m is None:
        return "-"
    return (f"n={m['n']:.0f} agree={m['agree']:.3f} bal={m['bal']:.3f}(sd {m['bal_sd']:.3f}) "
            f"rescue={m['rescue']:.2f} cleanspec={m['clean_spec']:.2f} "
            f"addP={m['add_prec']:.2f}(n={m['n_add']:.1f}) remP={m['rem_prec']:.2f}(n={m['n_rem']:.1f})")


def groups(recs):
    imgs = sorted({r["image"] for r in recs})
    out = [("pooled", {}), ("pooled near", {"set": "near"}), ("pooled far", {"set": "far"})]
    for im in imgs:
        for st in sorted({r["set"] for r in recs if r["image"] == im and "set" in r}):
            out.append((f"{im[:12]}/{st}", {"image": im, "set": st}))
    return out


def terciles(recs, key):
    mk = {}
    for r in recs:
        if r.get("arm") == "gate" and r.get(key) is not None:
            mk[(r["image"], r["marker"])] = r[key]
    vals = np.asarray(sorted(mk.values()))
    if vals.size < 3:
        return {}, []
    cuts = np.quantile(vals, [1 / 3, 2 / 3])
    return {k: int(np.searchsorted(cuts, v, side="right")) for k, v in mk.items()}, cuts.tolist()


def main():
    paths = [a for a in sys.argv[1:] if not a.startswith("--") and a.endswith(".json")]
    gate_arm = sys.argv[sys.argv.index("--arm") + 1] if "--arm" in sys.argv else "human"
    recs = collect(paths, gate_arm)
    errs = [r for r in recs if "error" in r]
    print(f"gate arm: {gate_arm}; pick errors: {len(errs)}", [(r['image'][:6], r['marker'], r['error'][:60]) for r in errs[:5]])
    recs = [r for r in recs if "error" not in r]
    for name, filt in groups(recs):
        print(f"\n== {name}")
        for arm in arms_list():
            print(f"{arm:30} {fmt(agg(recs, arm, **filt))}")
    if "--snr" in sys.argv:
        for key in ("d", "contrast"):
            tmap, cuts = terciles(recs, key)
            print(f"\n== terciles of {key}; cuts {cuts}")
            for t in range(3):
                members = [k for k, v in tmap.items() if v == t]
                print(f"-- tercile {t} ({len(members)} markers)")
                sub = [dict(r) for r in recs if tmap.get((r["image"], r["marker"])) == t]
                for arm in ("gate", "raw", "guc a=0.05 c=0.1 ag=None", "guc a=None c=0.1 ag=None"):
                    print(f"{arm:30} {fmt(agg(sub, arm))}")
    if "--dbins" in sys.argv:
        # post-hoc: d in fixed bins, the raw call and the extended margins
        bins = [(0, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 99)]
        print("\n== bins of d (fixed edges)")
        for lo, hi in bins:
            members = {(r["image"], r["marker"]) for r in recs if r.get("arm") == "gate" and lo <= r["d"] < hi}
            sub = [r for r in recs if (r["image"], r["marker"]) in members]
            print(f"-- d in [{lo}, {hi}): {len(members)} markers")
            for arm in [a for a in arms_list() if a in ("gate", "raw") or a.endswith("ag=None")]:
                m = agg(sub, arm)
                if m and (m["n_add"] or m["n_rem"] or arm in ("gate", "raw")):
                    print(f"{arm:30} {fmt(m)}")
    if "--markers" in sys.argv:
        print("\n== per marker (gate arm, seed-mean raw, add/clean precision of a=0.05 c=0.1)")
        seen = sorted({(r["image"], r["marker"]) for r in recs})
        for im, mk in seen:
            sub = [r for r in recs if r["image"] == im and r["marker"] == mk]
            g, ra = agg(sub, "gate"), agg(sub, "raw")
            u = agg(sub, "guc a=0.05 c=0.1 ag=None")
            r0 = next(r for r in sub if r["arm"] == "gate")
            print(f"{im[:8]:8} {mk:10} d={r0['d']:.2f} contrast={r0['contrast']} n={g['n']:.0f} "
                  f"gate_bal={g['bal']:.2f} raw_bal={ra['bal']:.2f} guc_bal={u['bal']:.2f} addP={u['add_prec']:.2f}(n={u['n_add']:.1f}) remP={u['rem_prec']:.2f}(n={u['n_rem']:.1f})")


if __name__ == "__main__":
    main()
