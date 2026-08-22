"""Is the ROI composition summary right?

The card says "this region is 42% macrophages", and nothing on screen can check
that. A wrong answer draws a card every bit as convincing as a right one, and
each way of getting it wrong looks like biology rather than a bug: counting a
few cells outside the polygon inflates a region; counting cells the column has
no row for shrinks every percentage uniformly, so nothing looks out of place;
folding the tail into `Other` with a remainder inferred from the visible bars
gives a number close enough to pass a glance and still wrong.

The Python suite never executes client JS and `node --check` sees syntax only,
so tests/js/cell_explorer_roi_bridge_probe.mjs runs the real bridge against a
lattice of cells whose membership is also computed by brute force -- the fast
path and the slow path have to agree. This runs it as part of the suite, and
then breaks the bridge on purpose to prove the probe would notice.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
STATIC = REPO_ROOT / "plexora" / "plugins" / "cell_explorer" / "static"
PROBE = REPO_ROOT / "tests" / "js" / "cell_explorer_roi_bridge_probe.mjs"


def _run(source=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    command = [str(node), str(PROBE)] + (["--source", str(source)] if source else [])
    proc = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, timeout=120)
    # The probe reports on stderr so its diagnostics never mix with output.
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def _mutate(tmp_path, old, new):
    source = (STATIC / "cellExplorerRoiBridge.js").read_text(encoding="utf-8")
    assert old in source, f"the code this test mutates has moved or been renamed: {old!r}"
    target = tmp_path / "cellExplorerRoiBridge.js"
    target.write_text(source.replace(old, new, 1), encoding="utf-8")
    return target


@pytest.fixture(scope="module")
def report():
    return _run()


def test_the_composition_of_a_region_is_correct(report):
    returncode, result = report
    assert not result["failures"], json.dumps(result["failures"], indent=2)
    assert returncode == 0


def test_the_probe_is_actually_checking_something(report):
    _, result = report
    assert result["checked"] >= 40


def test_the_probe_catches_a_bucket_range_that_is_one_short(tmp_path):
    """The grid index is what makes a hover cost the region's area rather than
    the slide's cell count. An off-by-one in the bucket range drops a whole
    column of cells, which is indistinguishable on screen from a region that
    happens to be sparse at one edge."""
    mutated = _mutate(
        tmp_path,
        "        const lastColumn = Math.max(0, Math.floor(box.maxX / span));",
        "        const lastColumn = Math.max(0, Math.floor(box.maxX / span) - 1);",
    )
    returncode, result = _run(mutated)
    assert returncode == 1
    assert result["failures"]


def test_the_probe_catches_membership_decided_by_the_bounding_box(tmp_path):
    """Dropping the point-in-polygon test leaves a summary of the region's
    BOUNDING BOX. For a rectangle that is the same answer, so it survives the
    obvious check -- and for every hand-drawn contour it is wrong."""
    mutated = _mutate(
        tmp_path,
        "if (RoiGeometry.containsPoint(geometry, x, y)) found.push(i);",
        "found.push(i);",
    )
    returncode, result = _run(mutated)
    assert returncode == 1
    assert result["failures"]


def test_the_probe_catches_cells_with_no_value_counted_in_the_total(tmp_path):
    """A cell this column has no row for is not part of this variable's
    population. Putting it in the denominator makes every percentage on the
    card smaller than it should be, uniformly -- the kind of wrong that never
    looks wrong."""
    mutated = _mutate(
        tmp_path,
        "            if (code < 0) continue;\n            total += 1;",
        "            total += 1;\n            if (code < 0) continue;",
    )
    returncode, result = _run(mutated)
    assert returncode == 1
    assert result["failures"]


def test_the_probe_catches_hidden_categories_counted_anyway(tmp_path):
    """The legend's checkboxes narrow the question being asked of the slide, so
    the card has to answer the narrowed one. Counting a hidden category anyway
    is invisible on the card -- the bar is not drawn either way -- and shows up
    only as every remaining bar being slightly too short, which reads as the
    region's composition rather than as a bug."""
    mutated = _mutate(
        tmp_path,
        "            if (hiddenLabels.has(label)) {\n"
        "                hidden += 1;\n"
        "                continue;\n"
        "            }",
        "",
    )
    returncode, result = _run(mutated)
    assert returncode == 1
    assert result["failures"]


def test_the_probe_catches_a_total_that_still_includes_hidden_cells(tmp_path):
    """The rows can be right and the total still wrong. Bars are drawn as a
    share of it, so a total counting cells no bar stands for leaves every bar
    shorter than the share of the picture it represents -- and two regions can
    no longer be compared by eye, which is what the fixed 0-100% track is for.
    """
    mutated = _mutate(
        tmp_path,
        "        let total = 0;\n        for (const row of shown.rows) total += row.count;",
        "        const total = tally.total;",
    )
    returncode, result = _run(mutated)
    assert returncode == 1
    assert result["failures"]
