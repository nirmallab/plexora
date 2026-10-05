"""QA a render: unsettled frames, black/blank frames, hot (possibly oversaturated) frames.

    python check_frames.py out/<name>.frames.jsonl [out/preview]

- unsettled: frames the recorder captured before tiles/overlays finished.
- dark: preview frames whose mean luminance is under 12/255 (black mid-flight
  tiles, a fade that went on too long) -- fades at the very start/end are fine.
- hot: preview frames where over 5 % of pixels have a channel at 250+.
  A prompt to look, not a verdict: white outlines, UI text and a dense
  aggregate of true positives all count. What is wrong is colours summing
  to white/pink and positives losing their cell structure.
Look at every flagged preview yourself before re-rendering.
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

rows = [json.loads(l) for l in open(sys.argv[1])]
bad = [r["n"] for r in rows if not r["ok"]]
print(f"{len(rows)} frames, {len(bad)} unsettled{': ' + str(bad[:20]) if bad else ''}")
prev = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(sys.argv[1]).parent / "preview"
for f in sorted(prev.glob("f*.png")):
    im = np.asarray(Image.open(f).convert("RGB").resize((480, 270)), dtype=np.float32)
    lum = float((im @ [0.2126, 0.7152, 0.0722]).mean())
    clip = float((im.max(axis=2) >= 250).mean())
    flags = (["dark"] if lum < 12 else []) + (["hot"] if clip > 0.05 else [])
    if flags:
        print(f"{f.name}: {' '.join(flags)} (mean {lum:.0f}, at 250+ {clip:.1%})")
