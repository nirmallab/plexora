"""P9's correctness rules: one session dump (page/py/p9_run.py) against
another, the first the reference (CPython).

    python compare_p9.py REFERENCE.json RUN.json

1. the packet kind sequence is identical -- hard;
2. every packet's normalized JSON and the Oracle's answer to it are equal,
   floats within rtol 1e-6 -- hard;
3. every image decodes to the same shape within max |d| <= 2 levels and
   mean |d| <= 0.5 -- soft (encoders may differ by version; bytes are
   reported, not required);
4. the final units: the same state, the final gate within rtol 1e-6 -- hard;
5. get_all_gates, normalized, equal within rtol 1e-6 -- soft.

`verdict` is "identical" (every rule, bytes too), "within" (rules 1-5),
"soft" (1, 2, 4 hold; 3 or 5 does not) or "different".
"""

from __future__ import annotations

import base64
import io
import json
import math
import sys

RTOL = 1e-6
IMAGE_MAX = 2
IMAGE_MEAN = 0.5


def _close(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
            return True
        return math.isclose(a, b, rel_tol=RTOL, abs_tol=1e-12)
    return a == b


def first_difference(a, b, where=""):
    """The first path at which `a` and `b` differ beyond RTOL, or None."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{where}/{key} (only in {'reference' if key in a else 'run'})"
            found = first_difference(a[key], b[key], f"{where}/{key}")
            if found:
                return found
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{where} (length {len(a)} != {len(b)})"
        for i, (x, y) in enumerate(zip(a, b)):
            found = first_difference(x, y, f"{where}[{i}]")
            if found:
                return found
        return None
    if not _close(a, b):
        return f"{where}: {json.dumps(a)[:120]} != {json.dumps(b)[:120]}"
    return None


def _pixels(image):
    import numpy as np
    from PIL import Image

    return np.asarray(Image.open(io.BytesIO(base64.b64decode(image["b64"]))).convert("RGBA"),
                      dtype=np.int16)


def _images(ref, run):
    out = {"count": 0, "bytesIdentical": 0, "within": 0, "maxAbs": 0, "maxMean": 0.0,
           "shapes": True, "worst": None}
    for i, (x, y) in enumerate(zip(ref["exchanges"], run["exchanges"])):
        for j, (a, b) in enumerate(zip(x["images"], y["images"])):
            out["count"] += 1
            if a["sha256"] == b["sha256"]:
                out["bytesIdentical"] += 1
                out["within"] += 1
                continue
            pa, pb = _pixels(a), _pixels(b)
            if pa.shape != pb.shape:
                out["shapes"] = False
                out["worst"] = out["worst"] or f"packet {i} image {j}: {pa.shape} != {pb.shape}"
                continue
            diff = abs(pa - pb)
            mx, mean = int(diff.max()), float(diff.mean())
            out["maxAbs"] = max(out["maxAbs"], mx)
            out["maxMean"] = max(out["maxMean"], mean)
            if mx <= IMAGE_MAX and mean <= IMAGE_MEAN:
                out["within"] += 1
            elif out["worst"] is None:
                out["worst"] = f"packet {i} ({x['kind']}) image {j}: max {mx}, mean {mean:.3f}"
    return out


def compare(ref, run):
    kinds_ref = [e["kind"] for e in ref["exchanges"]]
    kinds_run = [e["kind"] for e in run["exchanges"]]
    packets = {"count": len(kinds_ref), "kindsIdentical": kinds_ref == kinds_run,
               "identical": 0, "firstDifference": None}
    for i, (x, y) in enumerate(zip(ref["exchanges"], run["exchanges"])):
        found = first_difference({"packet": x["packet"], "answer": x["answer"]},
                                 {"packet": y["packet"], "answer": y["answer"]})
        if found is None:
            packets["identical"] += 1
        elif packets["firstDifference"] is None:
            packets["firstDifference"] = f"packet {i} ({x['kind']}): {found}"
    if not packets["kindsIdentical"] and packets["firstDifference"] is None:
        packets["firstDifference"] = f"kinds {kinds_ref} != {kinds_run}"
    images = _images(ref, run)
    units = {"count": len(ref["units"]), "statesIdentical": True, "maxRel": 0.0, "within": True}
    for marker, u in ref["units"].items():
        v = run["units"].get(marker) or {}
        if u.get("state") != v.get("state"):
            units["statesIdentical"] = False
        a, b = u.get("final"), v.get("final")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            rel = abs(a - b) / max(abs(a), 1e-12)
            units["maxRel"] = max(units["maxRel"], rel)
            if rel > RTOL:
                units["within"] = False
        elif a != b:
            units["within"] = False
    gates_diff = first_difference(ref["gates"], run["gates"])
    hard = (packets["kindsIdentical"] and packets["identical"] == packets["count"]
            and units["statesIdentical"] and units["within"])
    soft = images["shapes"] and images["within"] == images["count"] and gates_diff is None
    identical = images["bytesIdentical"] == images["count"] and units["maxRel"] == 0.0
    verdict = ("identical" if hard and soft and identical else "within" if hard and soft
               else "soft" if hard else "different")
    return {"verdict": verdict, "packets": packets, "images": images, "units": units,
            "gates": {"identical": gates_diff is None, "firstDifference": gates_diff},
            "modules": _modules(ref, run)}


#: Imported by CPython's run but never by the browser's for a reason that is
#: not a fallback: the threads Pyodide does not have, Windows' own modules,
#: and the Flask app the stub root does not build.
_EXPECTED_ABSENT = ("threading", "_thread", "concurrent.futures.thread", "multiprocessing",
                    "nt", "msvcrt", "winreg", "_winapi", "ctypes.wintypes", "flask", "werkzeug",
                    "jinja2", "plexora.server.routes", "plexora.telemetry.flask_hooks")


def _modules(ref, run):
    """Top-level packages CPython's run imported that this run did not."""
    if "modules" not in ref or "modules" not in run:
        return None
    have = set(run["modules"])
    missing = sorted({m.split(".")[0] if not m.startswith("plexora.") else ".".join(m.split(".")[:4])
                      for m in ref["modules"] if m not in have
                      and not m.startswith(_EXPECTED_ABSENT)})
    return {"missingInRun": missing, "count": len(missing)}


def main(argv=None):
    argv = argv or sys.argv[1:]
    ref, run = (json.loads(open(p, encoding="utf-8").read()) for p in argv[:2])
    ref = ref.get("dump", ref)
    run = run.get("dump", run)
    print(json.dumps(compare(ref, run), indent=1))


if __name__ == "__main__":
    main()
