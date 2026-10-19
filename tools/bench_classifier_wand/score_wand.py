"""Score bench_wand.py results: python score_wand.py OUT1.json [OUT2.json ...] [--per-marker]"""
import json
import sys

import numpy as np


def bal(y, p):
    y, p = np.asarray(y), np.asarray(p)
    parts = [np.mean(p[y == c] == c) for c in (0, 1) if (y == c).any()]
    return float(np.mean(parts)) if parts else float("nan")


def main():
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    per_marker = "--per-marker" in sys.argv
    rows = {}
    for path in paths:
        d = json.load(open(path))
        proj = d["project"][:12]
        for marker, e in d["markers"].items():
            y = np.asarray(e["y"])
            sets = np.asarray(e["set"])
            for subset in sorted(set(sets.tolist())):
                m = sets == subset
                key = (proj, subset)
                r = rows.setdefault(key, {})
                g = r.setdefault("gate(gmm)", {"y": [], "p": [], "mk": []})
                if e["gate_call"] is not None:
                    g["y"] += y[m].tolist(); g["p"] += np.asarray(e["gate_call"])[m].tolist(); g["mk"] += [marker] * int(m.sum())
                k = r.setdefault("gate(label)", {"y": [], "p": [], "mk": []})
                k["y"] += y[m].tolist(); k["p"] += np.asarray(e["key_call"])[m].tolist(); k["mk"] += [marker] * int(m.sum())
                for arm, a in e["arms"].items():
                    acc = r.setdefault(arm, {"y": [], "p": [], "u": [], "amber": [], "stab": [], "mk": [], "err": 0})
                    for s in a["seeds"]:
                        if "error" in s:
                            acc["err"] += 1
                            continue
                        acc["y"] += y[m].tolist(); acc["p"] += np.asarray(s["pred"])[m].tolist()
                        acc["u"] += np.asarray(s["uncertain"])[m].tolist(); acc["mk"] += [marker] * int(m.sum())
                        acc["amber"].append(s["amber_share"])
                    if a["stability"] is not None:
                        acc["stab"].append(a["stability"])
    print(f"{'image/set':22} {'arm':12} {'n':>5} {'acc':>6} {'bal':>6} {'amber@lab':>9} {'acc certain':>11} {'amber all':>9} {'stable':>7} err")
    pooled = {}
    for key in sorted(rows):
        for arm, v in rows[key].items():
            y, p = np.asarray(v["y"]), np.asarray(v["p"])
            if y.size == 0:
                continue
            u = np.asarray(v.get("u", [0] * y.size)).astype(bool)
            line = f"{key[0] + '/' + key[1]:22} {arm:12} {y.size:5d} {np.mean(y == p):6.3f} {bal(y, p):6.3f}"
            if "u" in v:
                cert = ~u
                line += f" {u.mean():9.3f} {np.mean(y[cert] == p[cert]) if cert.any() else float('nan'):11.3f}"
                line += f" {np.mean(v['amber']):9.3f} {np.mean(v['stab']) if v['stab'] else float('nan'):7.3f} {v['err']}"
            print(line)
            P = pooled.setdefault((key[1], arm), {"y": [], "p": [], "u": []})
            P["y"] += v["y"]; P["p"] += v["p"]; P["u"] += v.get("u", [0] * len(v["y"]))
        print()
    print("POOLED")
    for (subset, arm), v in sorted(pooled.items()):
        y, p, u = np.asarray(v["y"]), np.asarray(v["p"]), np.asarray(v["u"]).astype(bool)
        print(f"{subset:5} {arm:12} n={y.size:5d} acc={np.mean(y == p):.3f} bal={bal(y, p):.3f} amber@lab={u.mean():.3f} acc_certain={np.mean(y[~u] == p[~u]) if (~u).any() else float('nan'):.3f}")
    if per_marker:
        print("\nPER MARKER (acc, near set)")
        for key in sorted(rows):
            if key[1] != "near":
                continue
            arms = list(rows[key])
            markers = sorted(set(rows[key]["gate(label)"]["mk"]))
            print(key[0], " ".join(f"{a[:9]:>9}" for a in arms))
            for mk in markers:
                vals = []
                for a in arms:
                    v = rows[key][a]
                    sel = np.asarray(v["mk"]) == mk
                    vals.append(np.mean(np.asarray(v["y"])[sel] == np.asarray(v["p"])[sel]) if sel.any() else float("nan"))
                print(f"  {mk:8}", " ".join(f"{x:9.2f}" for x in vals))


main()
