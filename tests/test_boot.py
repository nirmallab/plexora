"""The website's boot (plexora/browser/boot.py), in a fresh CPython.

What the worker does once Pyodide is up, with Python functions where the page's
would be: the app builds with cooperative work and without imagecodecs, the
page's requests are answered through the bridge, a viewer session's change
wakes the page, and a job a request queued runs only when the worker steps.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r"""
import importlib.abc, json, sys
class Absent(importlib.abc.MetaPathFinder):
    # As in Pyodide: no imagecodecs, no numba.
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("imagecodecs", "numba"):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
sys.meta_path.insert(0, Absent())

from plexora.browser import boot, bridge, cooperative
posted = []
asked = []

def page(kind, args):
    # The page's answers (services/onnxExecutor.js for "sam"): its runtime is
    # there, the model's files are not yet.
    asked.append((kind, args.get("op")))
    if kind == "sam" and args.get("op") == "status":
        names = [f["name"] for f in args["model"]["files"]]
        return {"runtime": {"installed": True, "version": "onnxruntime-web"},
                "weights": {"files": {n: {"present": False, "ok": False} for n in names}, "missing": names}}
    return None

info = boot.start("https://viewer.plexoraapp.com", post=posted.append,
                  data_root=sys.argv[1], licence_dir=sys.argv[2], ask=page)
import plexora
from plexora import _work
app = plexora.app
out = {"info": info, "mode": _work.mode()}

def ask(method, path, body=None):
    answer = bridge.handle(app, {"method": method, "path": path,
                                 "headers": {"Content-Type": "application/json"},
                                 "body": json.dumps(body).encode() if body is not None else b""},
                           "https://viewer.plexoraapp.com")
    return answer["status"], (json.loads(answer["body"]) if answer["body"] else None)

out["projects"] = ask("GET", "/projects")[0]
status, session = ask("POST", "/agent/v1/viewer/sessions", {"project": None})
out["register"] = status
sid = session["session"]["view_id"]
before = len([m for m in posted if m["kind"] == "wakeup"])
from plexora.server.models import viewer_sessions
viewer_sessions.enqueue(sid, "pan_to", {"x": 1})
out["woken"] = len([m for m in posted if m["kind"] == "wakeup"]) - before

ran = []
_work.spawn(ran.append, "job")
out["before_step"] = list(ran)
out["step"] = cooperative.step(1000)
out["after_step"] = list(ran)
import tifffile
out["tifffile_codecs"] = tifffile.tifffile.imagecodecs.__name__
# Magic select's model is the page's (plexora/browser/sam_page.py): its status
# is asked of the page, in the desktop's states.
out["segment_status"] = ask("GET", "/segment/v1/status")[1]["state"]
out["segment_asked"] = [entry for entry in asked if entry[0] == "sam"]
print(json.dumps(out))
"""


def test_the_worker_boots_and_answers_through_the_bridge(tmp_path):
    env = {**os.environ}
    for name in ("PLEXORA_WORK", "PLEXORA_DATA_PATH", "BIOCOGNIA_DIR", "PLEXORA_PLUGINS"):
        env.pop(name, None)
    data, licence = tmp_path / "data", tmp_path / "bioc"
    data.mkdir()
    licence.mkdir()
    done = subprocess.run([sys.executable, "-c", SCRIPT, str(data), str(licence)], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-3000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["mode"] == "cooperative"
    assert out["info"]["shims"]["imagecodecs"] is True
    assert out["info"]["rules"] > 300
    assert out["projects"] == 200 and out["register"] == 200
    assert out["woken"] >= 1
    assert out["before_step"] == [] and out["after_step"] == ["job"]
    assert out["step"]["ran"] == 1 and out["step"]["pending"] == 0
    assert out["tifffile_codecs"] == "plexora.browser.imagecodecs_shim"  # tifffile took the stand-in
    assert out["segment_status"] == "weights_missing"
    assert out["segment_asked"] == [["sam", "status"]]
