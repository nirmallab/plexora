"""Two tools on screen at once, and only the two that asked to be.

Single-active is the rule everywhere else in the sidebar. Cell Explorer's Open
ROIs button is the one exception: an ROI composition card summarises the cells
under a metadata overlay, so folding the overlay away answers a question about
a picture the user can no longer see.

An exception is only safe while it stays narrow, and every way it could stop
being narrow is silent -- a pair that collapses back to one tool, a pair that
outlives the third tool that should have ended it, a half-closed pair that
leaves `activeToolName` null with a panel still expanded. Nothing throws, and
nothing here is visible to a test that renders one panel at a time.

tests/js/tool_coexist_probe.mjs drives the real toolLoader against a DOM stand
-in. This runs it as part of the suite, and then breaks the code on purpose to
prove the probe would notice -- a probe that passes whatever the code does is
worth nothing.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "tool_coexist_probe.mjs"
TOOL_LOADER = REPO_ROOT / "plexora" / "client" / "src" / "js" / "views" / "toolLoader.js"

#: The short-circuit that IS the exception. Without it the pair collapses back
#: to single-active and nothing reports it.
PAIR_SURVIVES_A_SWITCH = (
    "if (except && isCoexisting(previous) && coexistPair.has(except)) return;"
)

#: Folding the second half when a third tool takes over. Without it the
#: exception outlives the pairing that justified it.
BOTH_HALVES_FOLD = "        if (partner) fold(partner);"


def _run_probe(source=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    command = [str(node), str(PROBE)] + (["--source", str(source)] if source else [])
    proc = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, timeout=120)
    # The probe reports on stderr so its own diagnostics never mix with output.
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def _mutate(tmp_path, old, new):
    source = TOOL_LOADER.read_text(encoding="utf-8")
    assert old in source, f"the code this test mutates has moved or been renamed: {old!r}"
    target = tmp_path / "toolLoader.js"
    target.write_text(source.replace(old, new, 1), encoding="utf-8")
    return target


@pytest.fixture(scope="module")
def probe_report():
    return _run_probe()


def test_two_tools_can_share_the_screen(probe_report):
    returncode, report = probe_report
    assert not report["failures"], json.dumps(report["failures"], indent=2)
    assert returncode == 0


def test_the_probe_is_actually_checking_something(probe_report):
    _, report = probe_report
    assert report["checked"] >= 20


def test_the_probe_catches_a_pair_that_collapses(tmp_path):
    """Without the short-circuit, opening ROI from Cell Explorer folds Cell
    Explorer away -- and the composition card then describes an overlay that is
    no longer drawn, which looks like a card bug rather than a loader one."""
    mutated = _mutate(tmp_path, PAIR_SURVIVES_A_SWITCH, "if (false) return;")
    returncode, report = _run_probe(mutated)
    assert returncode == 1
    assert report["failures"]


def test_the_probe_catches_a_pair_that_outlives_its_reason(tmp_path):
    """Opening a third tool has to fold BOTH halves. Leaving one drawn is a
    stacked layer nobody asked to keep, with nothing on screen to explain it."""
    mutated = _mutate(tmp_path, BOTH_HALVES_FOLD, "")
    returncode, report = _run_probe(mutated)
    assert returncode == 1
    assert report["failures"]
