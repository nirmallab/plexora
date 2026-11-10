"""The website's workspace on disk (client services/browserStore.js): the folder
the user picks is the workspace, read for what it is before anything is
written, made one in an order that never leaves half of it, and opened again
on a later visit without a question.

The checks are in tests/js/browser_store_probe.mjs, against a fake File System
Access tree; this wrapper runs it and pins its lines.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "browser_store_probe.mjs"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than silently reducing what is covered.
CHECKS = (
    "the picked folder is returned as it is: nothing is made inside it",
    "a cancelled dialog is null; a refusal is `denied`; no dialog is `unsupported`",
    "a folder with workspace.json is a workspace, with its id",
    "a projects/config.json alone is a workspace too, its projects listed",
    "the library an old version made inside is legacy nested, and named for both folders",
    "a workspace's own projects folder is told apart",
    "only the system's own files is empty; anything else is foreign",
    "inspecting writes nothing",
    "adopting a folder makes it the root: Imaging/{workspace.json, projects, pyramids}, no Imaging/Plexora",
    "a folder named Plexora is the one the desktop app finds",
    "workspace.json is written only where there is none",
    "a legacy nested library is adopted where it is: nothing moved, the outer folder untouched",
    "what the browser holds is merged in; a project of the same name there is kept; caches never",
    "a folder that cannot be written is found before anything is made in it",
    "a full disk is `quota`",
    "a folder that is not a workspace is not made one without `init`; a projects folder never",
    "a folder the page may not change is asked for, and a refusal is `denied`",
    "the choice is remembered before Python is waited for; Python is moved once it has started",
    "prepare never asks: no record is a first visit, and onboarding is due",
    "an old record keeps working: its folder opens, labelled as it was",
    "a folder the browser no longer grants is left closed, with no question, until a click",
    "Reconnect asks the browser, brings this visit's work, and moves Python there",
    "Continue with browser storage sets the folder aside for this visit only",
    "browser storage is made and written to once, then remembered as the choice",
    "useFolder and switchTo go through the same adoption",
    "other workspaces are kept by handle: added once, listed after the default, removed",
    "making another workspace the default moves Python there and keeps the old one listed",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run([node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=120)


def test_the_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert line in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert f"{len(CHECKS)} checks passed" in probe.stdout


def test_kept_out_matches_the_store_sync():
    """browserStore.js repeats storeSync's list (it is a classic script and
    cannot import it): the two must name the same entries."""
    import re

    js = REPO_ROOT / "plexora" / "client" / "src" / "js"
    pattern = re.compile(r"const KEPT_OUT = new Set\(\[([^\]]*)\]\)")
    sync = pattern.search((js / "workers" / "storeSync.js").read_text(encoding="utf-8")).group(1)
    store = pattern.search((js / "services" / "browserStore.js").read_text(encoding="utf-8")).group(1)
    assert sorted(re.findall(r'"([^"]+)"', sync)) == sorted(re.findall(r'"([^"]+)"', store))
    prefixes = re.compile(r"const KEPT_OUT_PREFIXES = \[([^\]]*)\]")
    sync = prefixes.search((js / "workers" / "storeSync.js").read_text(encoding="utf-8")).group(1)
    store = prefixes.search((js / "services" / "browserStore.js").read_text(encoding="utf-8")).group(1)
    assert sorted(re.findall(r'"([^"]+)"', sync)) == sorted(re.findall(r'"([^"]+)"', store))
    assert ".importing-" in sync and ".bundles" in sync and ".replaced" not in sync
