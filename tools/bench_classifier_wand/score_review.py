import json, sys
import numpy as np
arms = ["gate", "base", "random4", "uncertain4", "random8", "uncertain8"]
tot = {a: [0, 0] for a in arms}
for path in sys.argv[1:]:
    d = json.load(open(path))
    per = {a: [0, 0] for a in arms}
    for r in d["rows"]:
        y = np.asarray(r["yB"])
        for a in arms:
            if r.get(a) is None:
                continue
            p = np.asarray(r[a])
            per[a][0] += int((p == y).sum()); per[a][1] += y.size
            tot[a][0] += int((p == y).sum()); tot[a][1] += y.size
    print(d["project"][:14], " ".join(f"{a}={per[a][0] / max(per[a][1], 1):.3f}" for a in arms))
print("POOLED        ", " ".join(f"{a}={tot[a][0] / max(tot[a][1], 1):.3f}" for a in arms), "n", tot["base"][1])
