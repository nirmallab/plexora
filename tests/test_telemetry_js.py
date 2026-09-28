"""The browser half of telemetry, run in node against the shipped scripts.

The checks live in tests/js/telemetry_*_probe.mjs; this wrapper runs them and
pins their lines, so a check that is deleted or renamed fails here rather than
silently reducing what is covered. And one contract test: a body the real
scripts build must pass the server's own ingest validation.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.server.routes import telemetry_routes

REPO_ROOT = Path(__file__).resolve().parent.parent
JS = REPO_ROOT / "tests" / "js"
SOURCES = [REPO_ROOT / "plexora/client/src/js/services" / name
           for name in ("telemetry.js", "errors.js", "performanceTelemetry.js")]

PROBES = {
    "telemetry_probe.mjs": (
        "the bins are the server's: 16 is <16, 17 is 16-50, 5001 is >5k",
        "counts and histograms fold into one row per key and dims",
        "off keeps nothing and posts nothing",
        "calls before the server answers are kept, and posted once it says on",
        "a post goes only to this tab's own server, with only rows",
        "a feature not on the list is dropped in the tab",
        "somebody else's plugin is named by a hash, or the server's own label",
        "a failed post keeps its rows for the next one",
        "a refused post (400) is not retried",
        "hidden -> keepalive fetch; pagehide -> sendBeacon with a JSON Blob",
        "the notice appears when due, and OK tells the server it was seen",
        "no notice when it is not due",
    ),
    "telemetry_errors_probe.mjs": (
        "the basename survives only for Plexora's own scripts on this origin",
        "report logs the message and counts only the fingerprint",
        "the same failure has the same fingerprint whatever its message",
        "an unknown component or action is `other`",
        "twenty new fingerprints per window, then quiet",
        "uncaught errors and rejections are counted as window and promise",
        "off counts nothing but still logs",
    ),
    "telemetry_perf_probe.mjs": (
        "classify returns only {path, kind}",
        "kinds come from the path's shape",
        "a proxy prefix is `proxy`, a direct node is `direct`",
        "a resource entry becomes histograms, and no URL survives",
        "the GPU family is attached only in diagnostics, as a family",
        "the texture cache's counters fold as deltas",
    ),
}

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


@pytest.mark.parametrize("probe", sorted(PROBES))
def test_probe(probe):
    done = subprocess.run([node, str(JS / probe)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines()
             if line.strip().startswith("ok  ")]
    assert tuple(lines) == PROBES[probe]


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_syntax(source):
    done = subprocess.run([node, "--check", str(source)], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr


def test_a_body_the_tab_builds_passes_the_servers_schema():
    done = subprocess.run([node, str(JS / "telemetry_rows_probe.mjs")], capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    body = json.loads(done.stdout)
    checked = telemetry_routes.validate_ingest(body)
    assert checked is not None, json.dumps(body, indent=1)
    _viewer, rows = checked
    events = {event for event, _row in rows}
    assert events == {"tool.summary", "feature.summary", "render.summary", "error.fingerprint"}
    assert len(json.dumps(body)) < telemetry_routes.MAX_INGEST_BYTES
