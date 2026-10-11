"""QC's "not called" state in the browser, run in node against the shipped scripts.

A cell QC says cannot be called for a gated marker -- inside a QC region, or
the marker flagged unreliable there -- is neither positive nor negative: the
gate's mask holds 2 for it and the cell layer draws it grey. The checks live in
tests/js/gate_not_called_probe.mjs (evaluateGateMask's `notCalled` argument,
numericData's not-called cache, the `plexora:qc-changed` announcements from
QC's writes and the ROI store, and the gating panel's note); this pins their
lines, so a check that is deleted or renamed fails here rather than silently
reducing what is covered.

Drawing the grey identically on the GPU and the CPU is
tests/test_label_gpu_parity.py's; the viewer's side of the gate is
tests/test_label_gpu.py's.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "gate_not_called_probe.mjs"
SCRIPTS = (
    "plexora/client/src/js/views/labelGpu.js",
    "plexora/client/src/js/services/numericData.js",
    "plexora/plugins/qc/static/qcApi.js",
    "plexora/plugins/roi/static/roiState.js",
    "plexora/plugins/gating/static/gatingSidebarController.js",
)

CHECKS = (
    "without notCalled the mask and count are the gate's alone",
    "...and the same with null or an empty object",
    "a not-called row is 2, whether the gate passed it or not",
    "not-called bytes for a key the gates do not name are ignored",
    "multi-key: the gate is the AND, and not called for EITHER key is 2",
    "multi-key: a key without not-called bytes leaves its cells to the other",
    "the one-key and many-key loops agree on the same cells",
    "a gated key the table lacks is skipped by the gate, not by QC (as the server asks QC)",
    "a repeated id: counted once as positive or as not called, and not called wins",
    "an uncertain positive is 3 and stays in the count; never over not called or a negative",
    "without flags the mask is the gate's and QC's alone",
    "getNotCalled asks /get_not_called for the marker on this datasource, through plexoraUrl",
    "...and answers one byte per row",
    "a second ask is the cache's, not another request",
    "two asks at once share one request",
    "a failed request resolves to null -- the gate still draws -- and is not cached",
    "invalidateNotCalled forgets every marker",
    "and so does invalidateColumns: the bytes are per row",
    "kept NOT_CALLED_CACHE_SIZE markers deep, the least recently used out first",
    "PlexoraQcChanged.announce raises plexora:qc-changed with the datasource",
    "every QC write that can move its calls announces once it lands",
    "a read, a colour, a new empty category or a name does not",
    "a write the server refused does not",
    "an ROI edit to a QC region announces once it is saved, not before",
    "deleting one announces too: its category is read before it goes",
    "an edit to an ordinary ROI does not",
    "moving an ordinary ROI into a QC category does",
    "a save that did not land announces nothing",
    "with a fit and none not called the note is QC's words, unchanged",
    "with cells not called it ends \", N not called\"",
    "...and the tooltip says why they are grey",
    "without a fit, the not-called count alone",
    "with neither, the note is hidden, as it was",
    "the viewer's word that this layer's gate moved repaints it; another layer's does not",
    "the cell model's block is hidden for a marker with no gate",
    "before a run: one button to check the cells, no note, no toggle",
    "while it runs: its progress and Cancel",
    "after a run: what it would move, in its colours, and the toggle",
    "the toggle draws the model's codes, and again stops",
    "a model that agrees with the gate says so, with nothing to draw",
)

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_scripts_parse(script):
    done = subprocess.run([node, "--check", str(REPO_ROOT / script)], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-3000:]


def test_probe():
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS
