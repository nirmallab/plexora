"""Magic select is called "magic select" on screen, never by its model's name.

People read "magic select" and nothing else: no notice, button, menu, tooltip
or row names the segmentation model behind it. The technical value `sam`
(lower case, a stored provenance key) is allowed -- the panels translate it
into words -- but "SAM" as a word, or "Segment Anything", in any of the files
that put text on the screen for this feature is a leak. Comments are held to
the same rule, which keeps the check a plain text scan and means a string
cannot hide behind a comment-stripping heuristic.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

FILES = (
    "plexora/client/src/js/services/segmentService.js",
    "plexora/client/src/js/services/magicToolbar.js",
    "plexora/plugins/roi/static/roiTools.js",
    "plexora/plugins/roi/static/roiSidebarController.js",
    "plexora/plugins/roi/static/roiTree.js",
    "plexora/plugins/qc/static/qcDraw.js",
    "plexora/plugins/qc/static/qcSidebarController.js",
    "plexora/plugins/qc/static/qcHover.js",
    "plexora/plugins/roi/templates/roi/panel.html",
    "plexora/plugins/qc/templates/qc/panel.html",
)

FORBIDDEN = (
    re.compile(r"\bSAM\b"),
    re.compile(r"segment\s+anything", re.IGNORECASE),
)


@pytest.mark.parametrize("relative", FILES)
def test_the_model_is_never_named(relative):
    path = REPO_ROOT / relative
    assert path.is_file(), f"{relative} is missing -- update this list if it moved"
    hits = [
        f"{relative}:{number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if any(pattern.search(line) for pattern in FORBIDDEN)
    ]
    assert not hits, "the model is named where people read:\n" + "\n".join(hits)


def test_the_scan_would_catch_a_leak():
    """The patterns themselves: a lower-case provenance key passes, the name does not."""
    assert not any(p.search('region.method === "sam"') for p in FORBIDDEN)
    assert not any(p.search("SAMPLE sample_id") for p in FORBIDDEN)
    assert any(p.search('title: "Setting up SAM…"') for p in FORBIDDEN)
    assert any(p.search("powered by Segment Anything") for p in FORBIDDEN)
