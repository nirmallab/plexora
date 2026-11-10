"""The website's browser node when a project's spec changes under the tiles it
is reading (services/browserNode.js `check`, `recheck`, TiffPool), run in Node
with its workers stood in for (tests/js/browser_node_swap_probe.mjs).

The mask the page builds is attached when its pyramid is done: Python's spec
of the project then serves the mask and counts a new load. The node used to
close every reader of the project on that change, the image's with the tiles
it was reading, which then never answered: each held one of the viewer's six
tile slots for good (views/tileFetch.js), so the viewer timed them out after
90 s and every tile after waited behind them."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(__file__).parent / "js" / "browser_node_swap_probe.mjs"
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_tiles_being_read_when_the_mask_is_attached_are_answered(probe):
    assert probe["firstTile"] is True
    # While the mask is prepared its tiles are Python's.
    assert probe["maskWhilePending"] == {"ok": True, "value": None}
    assert probe["inFlightAnswered"] == ["ok", "ok", "ok"]
    assert probe["maskAfterAttach"] is True


def test_the_image_stays_open_when_only_the_mask_changed(probe):
    assert probe["imageOpens"] == 1
    assert probe["maskOpens"] == 1


def test_the_image_tiles_do_not_wait_for_the_masks_recheck(probe):
    assert probe["duringRecheck"] is True


def test_a_read_its_closed_reader_strands_fails_at_once(probe):
    # A different file: the old readers close, what they had not answered
    # fails (the viewer asks Python), and the new file's readers answer.
    assert probe["strandedRead"] == "failed"
    assert probe["nextAfterSwap"] is True
    assert probe["imageOpensAfterSwap"] == 2
