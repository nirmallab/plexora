"""Plexora AI and its jobs on the website, in a real Chrome.

- `tests/browser/ai_chat.mjs`: the chat panel against a stand-in platform and
  AI gateway (tests/browser/mock_platform.py) -- the browser licensed through
  Settings, a turn that sets a gate through a tool call, the answer drawn as
  it streams, polls answered by the page while the worker runs the turn,
  Undo from the chip.
- `tests/browser/jobs.mjs`: apply_gate_to_dataset, the QC panel's blur check
  and the Single Cell Classifier run as jobs in the website's Python, their
  progress answered by the page while they run, a cancel from the panel
  honoured mid-run -- and each result equal to what CPython computes here
  from the same fixture (the suite's own fixture modules, the same seeds).

Needs what a developer machine has and CI does not yet: system Chrome, node,
playwright-core (`npm ci` in tools/poc_browser_node) and a website bundle
built from this checkout (`tools/browser_build/build.py`, at least
`--plexora-only` after a change under plexora/), found under
PLEXORA_BENCH_HOME. Skipped, saying which, without them.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(os.environ.get("PLEXORA_BENCH_CHROME")
              or r"C:\Program Files\Google\Chrome\Application\chrome.exe")
BUILD = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "web" / "py"
PLAYWRIGHT = ROOT / "tools" / "poc_browser_node" / "node_modules" / "playwright-core"

SPEC = {"cohort": {"shifts": [0.0, 0.0, 0.4, 0.2, 0.1, 0.3], "grid": 16, "size": 384},
        "qc": {"size": 1024, "grid": 40, "artifacts": ["blur_local"], "channel": "DNA_2"},
        "gate": {"marker": "CD3", "low": 500.0}}


def _version():
    import plexora

    return plexora.__version__


def _prerequisites():
    missing = [what for what, there in (("Chrome", CHROME.exists()), ("node", shutil.which("node")),
                                        ("the build", (BUILD / _version() / "stages.json").exists()),
                                        ("playwright-core", PLAYWRIGHT.exists())) if not there]
    if missing:
        pytest.skip(f"needs {', '.join(missing)} (see this file's docstring)")


def _drive(script, *args, timeout=1200):
    env = {**os.environ, "PLEXORA_BENCH_HOME": str(BUILD.parents[2])}
    done = subprocess.run(["node", str(ROOT / "tests" / "browser" / script), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    start = next((i for i, line in enumerate(lines) if line.startswith("{")), None)
    out = json.loads("\n".join(lines[start:])) if start is not None else {}
    return done, out


def test_the_chat_streams_calls_a_tool_and_undoes_it_on_the_website():
    _prerequisites()
    done, out = _drive("ai_chat.mjs")
    assert done.returncode == 0 and out.get("ok"), json.dumps(out, indent=1)[-4000:] + done.stderr[-2000:]
    names = [c["name"] for c in out["checks"]]
    assert names == [
        "a project with a cell table is made in the website's Python",
        "the gateway was called from the worker with the page's token, as chat turns",
        "the tool call shows in the panel and the gate is set in Python",
        "the answer was drawn as it streamed",
        "the event stream was refused and the panel polled; polls were answered by the page while the turn ran",
        "Undo on the chip puts the gate back",
    ]


#: What differs between two runs of the same computation and is no part of
#: its result: ids, times, places on disk.
VOLATILE = ("operation_id", "receipt", "job_id", "seconds", "_ms", "elapsed", "_at", "path", "computed",
            "timing", "duration", "created", "updated", "revision", "run_id", "source_path", "fingerprint")


def _normal(value, digits=6):
    if isinstance(value, dict):
        return {k: _normal(v, digits) for k, v in sorted(value.items())
                if not any(mark in k for mark in VOLATILE)}
    if isinstance(value, list):
        return [_normal(v, digits) for v in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return float(f"{value:.{digits}g}")
    return value


def _desktop(tmp_path):
    """The same three computations in this process, on the same fixtures,
    asked of the same routes the website's page asks."""
    import plexora
    from plexora.agent import jobs
    from tests.autogate_fixtures import make_gating_dataset
    from tests.qc_fixtures import make_qc_project

    cohort, qc, gate = SPEC["cohort"], SPEC["qc"], SPEC["gate"]
    make_gating_dataset(tmp_path, "cohort", shifts=tuple(cohort["shifts"]), grid=cohort["grid"],
                        size=cohort["size"])
    make_qc_project(tmp_path, "qcsynth", size=qc["size"], grid=qc["grid"], artifacts=tuple(qc["artifacts"]))
    http = plexora.app.test_client()
    out = {}

    def capability(tool, arguments):
        answer = http.post(f"/agent/v1/capabilities/{tool}", json={"arguments": arguments}).get_json()
        assert answer["success"], answer
        return jobs.store().wait(answer["result"]["job_id"], 300)["result"]

    out["apply"] = capability("apply_gate_to_dataset", {"dataset": "cohort", "marker": gate["marker"],
                                                        "low": gate["low"]})
    run = http.post("/plugins/qc/blur/run", json={"datasource": "qcsynth", "channel": qc["channel"]}).get_json()
    jobs.store().wait(run["job_id"], 300)
    out["qc"] = http.get(f"/plugins/qc/blur?datasource=qcsynth&channel={qc['channel']}").get_json()
    out["classify"] = capability("classifier_run", {"project": "cohort_1", "marker": gate["marker"],
                                                    "auto_pick": True})
    return out


def test_jobs_run_on_the_website_as_on_the_desktop(tmp_path):
    _prerequisites()
    want = _desktop(tmp_path)
    done, out = _drive("jobs.mjs", "--spec", json.dumps(SPEC))
    assert done.returncode == 0 and out.get("ok"), json.dumps(out, indent=1)[-4000:] + done.stderr[-2000:]
    for name in ("apply", "qc", "classify"):
        assert _normal(out["results"][name]) == _normal(want[name]),             (name, _differences(_normal(out["results"][name]), _normal(want[name]))[:20])


def _differences(got, want, where=""):
    """The paths at which two normalised answers differ, with both values."""
    if isinstance(got, dict) and isinstance(want, dict):
        out = []
        for key in sorted(set(got) | set(want)):
            out += _differences(got.get(key, "<absent>"), want.get(key, "<absent>"), f"{where}.{key}")
        return out
    if isinstance(got, list) and isinstance(want, list) and len(got) == len(want):
        return [d for i, (g, w) in enumerate(zip(got, want)) for d in _differences(g, w, f"{where}[{i}]")]
    return [] if got == want else [(where, str(got)[:120], str(want)[:120])]
