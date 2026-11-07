"""Magic select on the website against the desktop: the CPython side.

    python tests/browser/magic_select_truth.py truth --file TIF --out DIR
    python tests/browser/magic_select_truth.py replay --out DIR
    python tests/browser/magic_select_truth.py compare --out DIR

`truth` makes a desktop project of the image in DIR/data (Plexora's own
`create_project`), chooses the prompts from the image itself (nuclei by their
Hoechst peaks, an immune cell by its CD45 peak, at four places), and sends
every case to `POST /segment/v1/segment` on the desktop's app, its model on
ONNX Runtime's CPU provider -- the desktop's answer on this machine. It
writes DIR/cases.json (what tests/browser/magic_select.mjs sends the
website), DIR/truth.json (the answers) and DIR/truth/<case>.npz (the image
embedding and the chosen mask's low-resolution logits that made each one).

magic_select.mjs sends the same cases to the website, once per runtime
(WebGPU, WebAssembly), and writes DIR/web-<runtime>.json and
DIR/web-<runtime>/<case>.npz the same way; a click it makes through the
viewer's own client is recorded as the request that client sent, and
`replay` sends those to the desktop's app (DIR/replay.json).

`compare` holds every website answer to the desktop's and prints one JSON
record; exit 1 when one is out of tolerance. What it requires, and why:

- **The same picture.** The composited crop the model saw has the same sha1
  on both sides (the website reads the pyramid its page built, tile-identical
  to the desktop's: tests/test_tiff_writer.py). Without it nothing else is a
  comparison of the model.
- **The model's numbers within float32 reordering.** Both run the same
  graph in float32; the website's kernels (WebAssembly SIMD128, or WebGPU
  shaders, whose WGSL allows a few ULP on exp and division) sum in other
  orders than MLAS on AVX2. Measured here: embeddings within ~4e-6 of values
  of order 1, logits within ~1e-3. The bars are 1e-3 on the embedding and
  0.05 on the logits (two to three orders of magnitude over what was
  measured, for other GPUs and drivers), and the predicted IoU within 1e-3.
- **The same mask.** A pixel may change sides only where the desktop's
  logit is within the logit bar of 0, i.e. on the outline; the chosen
  candidate (multimask) is the same; the polygons overlap with IoU >= 0.99
  and the flags, level and crop are equal.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = "C:/Users/aj/AppData/Local/plexora/.models/segment"

#: Tolerances (the module docstring says why).
EMBEDDING_ABS = 1e-3
LOGIT_ABS = 0.05
IOU_SCORE_ABS = 1e-3
POLYGON_IOU = 0.99

HOECHST = "01_Nucleus_Hoechst"
CD45 = "03_CD45_ArgoFluor520"
ONE = [{"name": HOECHST, "color": "#ffffff", "range": [200, 6000]}]
TWO = [{"name": HOECHST, "color": "#0000ff", "range": [200, 6000]},
       {"name": CD45, "color": "#00ff00", "range": [200, 9000]}]
#: Where the cases are, in full-resolution pixels of LSP11385's crop: an
#: epithelium, an immune infiltrate, a gland and a sparse field.
PLACES = {"epithelium": (2640, 1440), "infiltrate": (1440, 2640), "gland": (3640, 4120),
          "sparse": (2000, 5040)}

#: What the website runs after each case to show what the model saw and
#: made (`PlexoraPython.run`, which answers with the last line's value), with
#: TOKEN, PROJECT and PREV set before it; `dump` runs the same lines here.
DUMP = """
import base64, hashlib, io, json
import numpy as np
from plexora.vision import segment as _seg
from plexora.server.routes import segment_routes as _routes
from plexora.server.utils import source_image as _si
_entry = _seg._lookup(token=TOKEN)
with _si.SHELF.reader(_routes._session().image_data(PROJECT), view=None) as _source:
    _rgb, _rendered, _bg = _si.composite(_source, _entry.channels, _entry.level, _entry.lbox)
_buffer = io.BytesIO()
np.savez(_buffer, embedding=_entry.embedding.array, low=_entry.low_res[PREV])
_dumped = json.dumps({"rgb_sha1": hashlib.sha1(np.ascontiguousarray(_rgb).tobytes()).hexdigest(),
                      "rgb_shape": list(_rgb.shape), "npz": base64.b64encode(_buffer.getvalue()).decode()})
_dumped
""".strip()


def _setup(out: Path) -> None:
    """The desktop as this machine runs it, its data in `out/data` (set before
    Plexora is imported: the data root is fixed per process)."""
    os.environ["PLEXORA_DATA_PATH"] = str(out / "data")
    os.environ.setdefault("PLEXORA_SEGMENT_MODEL_DIR", os.environ.get("PLEXORA_TEST_SEGMENT_MODEL_DIR")
                          or MODEL_DIR)
    os.environ["PLEXORA_SEGMENT_DEVICE"] = "cpu"
    sys.path.insert(0, str(ROOT))


def dump(project: str, answer: dict) -> dict:
    scope = {"TOKEN": answer["token"], "PROJECT": project, "PREV": answer["prev_key"]}
    exec(DUMP, scope)  # noqa: S102 -- the same lines the website runs
    return json.loads(scope["_dumped"])


def save_npz(record: dict, path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(record.pop("npz")))
    return record


def _peaks(plane, centre, radius, count, sigma=3.0, spacing=15):
    """The `count` brightest blobs of `plane` nearest `centre` (x, y)."""
    import numpy as np
    from scipy import ndimage

    cx, cy = centre
    x0, y0 = max(0, cx - radius), max(0, cy - radius)
    crop = plane[y0:cy + radius, x0:cx + radius].astype(np.float32)
    smooth = ndimage.gaussian_filter(crop, sigma)
    peaks = (smooth == ndimage.maximum_filter(smooth, spacing)) & (smooth > np.percentile(smooth, 90))
    ys, xs = np.nonzero(peaks)
    order = np.argsort(np.hypot(xs + x0 - cx, ys + y0 - cy))[:count]
    return [(float(xs[i] + x0), float(ys[i] + y0)) for i in order]


def _view(centre, side):
    cx, cy = centre
    return {"x": float(cx - side / 2), "y": float(cy - side / 2), "width": float(side), "height": float(side)}


def choose_cases(file: str) -> list:
    """The cases, from the image: the requests and how they chain."""
    import tifffile

    with tifffile.TiffFile(file) as tif:
        planes = tif.series[0].asarray()
    hoechst, cd45 = planes[0], planes[2]
    a1, a2, a3 = _peaks(hoechst, PLACES["epithelium"], 120, 3)
    wide = _peaks(hoechst, PLACES["epithelium"], 700, 40)[::13][:3]
    immune = _peaks(cd45, PLACES["infiltrate"], 200, 1)[0]
    sparse = _peaks(hoechst, PLACES["sparse"], 40, 1)[0]
    gx, gy = PLACES["gland"]
    point = lambda xy, label=1: {"x": round(xy[0], 1), "y": round(xy[1], 1), "label": label}  # noqa: E731
    return [
        {"name": "point", "note": "one click on a nucleus: the multimask choice",
         "request": {"view": _view(PLACES["epithelium"], 512), "points": [point(a1)], "channels": ONE}},
        {"name": "grow", "note": "a second include point, on the first click's embedding",
         "token_from": "point",
         "request": {"view": _view(PLACES["epithelium"], 512), "points": [point(a1), point(a2)],
                     "channels": ONE}},
        {"name": "carve", "note": "an exclude point, from the last mask (use_prev_mask)",
         "token_from": "point", "prev_from": "grow",
         "request": {"view": _view(PLACES["epithelium"], 512),
                     "points": [point(a1), point(a2), point(a3, 0)], "channels": ONE,
                     "options": {"use_prev_mask": True}}},
        {"name": "box", "note": "a box round a gland's lumen",
         "request": {"view": _view((gx, gy), 800), "points": [], "channels": ONE,
                     "box": {"x": gx - 90.0, "y": gy - 90.0, "width": 180.0, "height": 180.0}}},
        {"name": "refine", "note": "an existing outline (the first click's) as the mask prompt, "
                                   "and a click on its neighbour",
         "token_from": "point", "mask_from": "point",
         "request": {"view": _view(PLACES["epithelium"], 512), "points": [point(a1), point(a2)],
                     "channels": ONE, "options": {}}},
        {"name": "two_channels", "note": "Hoechst blue and CD45 green, on an immune cell",
         "request": {"view": _view(PLACES["infiltrate"], 700), "points": [point(immune)],
                     "channels": TWO}},
        {"name": "small", "note": "a 100 px view, scaled up ten times to the model's 1024",
         "request": {"view": _view(sparse, 100), "points": [point(sparse)], "channels": ONE}},
        {"name": "wide", "note": "a 2000 px view: pyramid level 1, three nuclei",
         "request": {"view": _view(PLACES["epithelium"], 2000), "points": [point(p) for p in wide],
                     "channels": ONE, "options": {"multi": True}}},
    ]


def resolve(case: dict, answers: dict, fixed: dict | None = None) -> dict:
    """The request a case sends, given the answers before it: the token of the
    embedding it reuses and the mask it starts from are that side's own; an
    outline used as a prompt is the desktop's (`fixed`), so both are handed
    the same one."""
    body = json.loads(json.dumps(case["request"]))
    if case.get("token_from"):
        body["token"] = answers[case["token_from"]]["token"]
    if case.get("prev_from"):
        body.setdefault("options", {})["prev_key"] = answers[case["prev_from"]]["prev_key"]
    if case.get("mask_from"):
        body.setdefault("options", {})["mask_geometry"] = (fixed or answers)[case["mask_from"]]["geometry"]
    return body


def _client():
    import plexora

    return plexora.app.test_client()


def _post(client, project, body):
    answer = client.post("/segment/v1/segment", json={"datasource": project, **body})
    data = answer.get_json()
    if answer.status_code != 200:
        raise SystemExit(f"segment answered {answer.status_code}: {data}")
    return data


def truth(args) -> int:
    out = Path(args.out)
    _setup(out)
    from plexora import datasets

    project = datasets.create_project(args.file, exist_ok=True)
    cases = choose_cases(args.file)
    client = _client()
    status = client.get("/segment/v1/status").get_json()
    if status["state"] != "ready":
        raise SystemExit(f"the desktop's magic select is {status['state']}: {status.get('hint')}")
    answers, records = {}, []
    for case in cases:
        body = resolve(case, answers)
        answer = _post(client, project, body)
        answers[case["name"]] = answer
        seen = save_npz(dump(project, answer), out / "truth" / f"{case['name']}.npz")
        records.append({"name": case["name"], "body": body, "answer": answer, **seen})
    from plexora.vision import sam_weights

    (out / "cases.json").write_text(json.dumps({"file": str(args.file), "cases": cases, "dump": DUMP,
                                                "web_dir": sam_weights.WEB_DIR,
                                                "model_dir": os.environ["PLEXORA_SEGMENT_MODEL_DIR"]},
                                               indent=1), encoding="utf-8")
    device = records[0]["answer"].get("device")
    (out / "truth.json").write_text(json.dumps({"project": project, "device": device,
                                                "records": records}, indent=1), encoding="utf-8")
    print(json.dumps({"project": project, "cases": len(cases), "device": device,
                      "answers": {r["name"]: [r["answer"]["iou"], r["answer"]["area_px2"]] for r in records},
                      "timing": {r["name"]: r["answer"]["timing"] for r in records}}))
    return 0


def replay(args) -> int:
    """The requests the viewer's own client sent on the website, sent to the
    desktop's app (DIR/web-*.json `ui`)."""
    out = Path(args.out)
    _setup(out)
    from plexora import datasets

    project = datasets.create_project(json.loads((out / "cases.json").read_text())["file"], exist_ok=True)
    client = _client()
    replayed = {}
    for path in sorted(out.glob("web-*.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        for click in run.get("ui", []):
            body = {key: value for key, value in click["body"].items()
                    if key not in ("datasource", "token")}
            answer = _post(client, project, body)
            seen = save_npz(dump(project, answer), out / "replay" / f"{path.stem}-{click['name']}.npz")
            replayed[f"{path.stem}/{click['name']}"] = {"body": body, "answer": answer, **seen}
    (out / "replay.json").write_text(json.dumps(replayed, indent=1), encoding="utf-8")
    print(json.dumps({"replayed": list(replayed)}))
    return 0


# -- compare -----------------------------------------------------------------------


def _polygon_iou(a, b):
    from shapely.geometry import shape

    if a is None or b is None:
        return 1.0 if a is None and b is None else 0.0
    first, second = shape(a).buffer(0), shape(b).buffer(0)
    union = first.union(second).area
    return 1.0 if union == 0 else float(first.intersection(second).area / union)


def measure(desktop: dict, web: dict, desktop_npz: Path, web_npz: Path) -> dict:
    """One case's differences, and whether each is within its bar."""
    import numpy as np

    d_answer, w_answer = desktop["answer"], web["answer"]
    with np.load(desktop_npz) as d, np.load(web_npz) as w:
        d_emb, w_emb, d_low, w_low = d["embedding"], w["embedding"], d["low"], w["low"]
    flips = (d_low > 0) != (w_low > 0)
    near = np.abs(d_low[flips]).max() if flips.any() else 0.0
    out = {
        "same_picture": desktop["rgb_sha1"] == web["rgb_sha1"],
        "embedding_max_abs": float(np.abs(d_emb - w_emb).max()),
        "logit_max_abs": float(np.abs(d_low - w_low).max()),
        "low_res_flips": int(flips.sum()),
        "flip_max_desktop_logit": float(near),
        "iou_score_abs": abs(float(d_answer["iou"]) - float(w_answer["iou"])),
        "polygon_iou": round(_polygon_iou(d_answer["geometry"], w_answer["geometry"]), 6),
        "area_px2": [d_answer["area_px2"], w_answer["area_px2"]],
        "same_flags": d_answer["flags"] == w_answer["flags"],
        "same_crop": (d_answer["level"], d_answer["crop"]) == (w_answer["level"], w_answer["crop"]),
        "level": w_answer["level"],
    }
    out["ok"] = bool(out["same_picture"] and out["embedding_max_abs"] <= EMBEDDING_ABS
                     and out["logit_max_abs"] <= LOGIT_ABS and near <= LOGIT_ABS
                     and out["iou_score_abs"] <= IOU_SCORE_ABS
                     and out["polygon_iou"] >= POLYGON_IOU and out["same_flags"] and out["same_crop"])
    return out


def compare(args) -> int:
    out = Path(args.out)
    desktop = json.loads((out / "truth.json").read_text(encoding="utf-8"))
    by_name = {record["name"]: record for record in desktop["records"]}
    replayed = json.loads((out / "replay.json").read_text(encoding="utf-8")) \
        if (out / "replay.json").exists() else {}
    report = {"tolerances": {"embedding_abs": EMBEDDING_ABS, "logit_abs": LOGIT_ABS,
                             "iou_score_abs": IOU_SCORE_ABS, "polygon_iou": POLYGON_IOU},
              "desktop_device": desktop["device"], "runs": {}}
    ok = True
    for path in sorted(out.glob("web-*.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        cases = {}
        for record in run["records"]:
            cases[record["name"]] = measure(by_name[record["name"]], record,
                                            out / "truth" / f"{record['name']}.npz",
                                            out / path.stem / f"{record['name']}.npz")
        for click in run.get("ui", []):
            key = f"{path.stem}/{click['name']}"
            if key in replayed:
                cases[f"ui:{click['name']}"] = measure(replayed[key], click,
                                                       out / "replay" / f"{path.stem}-{click['name']}.npz",
                                                       out / path.stem / f"ui-{click['name']}.npz")
        worst = {field: max(case[field] for case in cases.values())
                 for field in ("embedding_max_abs", "logit_max_abs", "iou_score_abs", "low_res_flips")}
        worst["polygon_iou_min"] = min(case["polygon_iou"] for case in cases.values())
        run_ok = all(case["ok"] for case in cases.values())
        ok = ok and run_ok
        report["runs"][path.stem] = {"ok": run_ok, "device": run.get("device"), "worst": worst,
                                     "cases": cases}
    report["ok"] = ok and bool(report["runs"])
    print(json.dumps(report, indent=1))
    return 0 if report["ok"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("truth")
    one.add_argument("--file", required=True)
    one.add_argument("--out", required=True)
    commands.add_parser("replay").add_argument("--out", required=True)
    commands.add_parser("compare").add_argument("--out", required=True)
    args = parser.parse_args(argv)
    return {"truth": truth, "replay": replay, "compare": compare}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
