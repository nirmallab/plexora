"""Display windows from a marker's own cells, so positives look positive.

    python windows.py <cells.csv> MARKER=GATE [MARKER=GATE ...] [--ref 0.40]

GATE is in gating units (log1p of the raw mean intensity, what the
Thresholding panel shows). For each marker:
  lo = 90th percentile of the negatives' raw intensity (background goes black)
  hi = where the median positive draws at 65 % (a cell just over the gate is lit)
  ref_hi = the same at 40 % -- use it when the marker is the dim REFERENCE
           channel beside another marker (co-expressed references dimmer still).
Raw units are what HD mode's slider windows use. These are starting points:
check every combination on its own field before filming (SKILL.md, colour).
Without gates (a tutorial with no gating), use the plexora MCP
`calibrate_display` instead.
"""
import json
import sys

import numpy as np
import pandas as pd

args = [a for a in sys.argv[1:] if not a.startswith("--")]
if len(args) < 2:
    sys.exit(__doc__)
ref_frac = float(sys.argv[sys.argv.index("--ref") + 1]) if "--ref" in sys.argv else 0.40
gates = {k: float(v) for k, v in (a.split("=", 1) for a in args[1:])}
cells = pd.read_csv(args[0], usecols=list(gates))
out = {}
for m, g in gates.items():
    raw = cells[m].to_numpy(float)
    pos = np.log1p(raw) >= g
    if pos.sum() < 10:
        out[m] = {"note": f"only {int(pos.sum())} positives: no window from cells, set by eye"}
        continue
    lo = float(np.percentile(raw[~pos], 90))
    med = float(np.median(raw[pos]))
    out[m] = {"m": [round(lo), round(lo + (med - lo) / 0.65)],
              "ref": [round(lo), round(lo + (med - lo) / ref_frac)],
              "pos_frac": round(float(pos.mean()), 3), "median_pos": round(med)}
print(json.dumps(out, indent=1))
