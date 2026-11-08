"""The website's project store in Plexora's folder on the computer, which the
desktop app writes too (client workers/storeSync.js, `open(handle, {shared})`).

The page holds its whole store in memory, and the desktop app may keep its
data root in that folder: so a shared folder's caches of one machine (the web
chunk cache, the .ims chunk indexes, model weights, the telemetry queue), the
desktop's server token, write probes and files too big to hold are never read
in, written or deleted from the page. The checks run in node against the
shipped file with an in-memory file system and folder handles
(tests/js/store_sync_shared_probe.mjs); the worker opens Plexora's folder
shared and its own storage not.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "store_sync_shared_probe.mjs"
WORKER = REPO_ROOT / "plexora" / "client" / "src" / "js" / "workers" / "pythonWorker.js"
STORE_SYNC = REPO_ROOT / "plexora" / "client" / "src" / "js" / "workers" / "storeSync.js"
PATHS = REPO_ROOT / "plexora" / "paths.py"


def test_a_shared_folder_keeps_what_is_not_the_pages():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert "store sync shared probe OK" in done.stdout


def test_the_worker_opens_plexoras_folder_shared():
    source = WORKER.read_text(encoding="utf-8")
    assert 'store.open(handle, { shared: kind === "folder" })' in source
    assert "store.open(folder, { shared: true })" in source


def test_kept_out_names_match_the_data_roots_caches():
    """Each cache name storeSync keeps out is the one plexora/paths.py names."""
    kept = re.search(r"const KEPT_OUT = new Set\(\[([^\]]*)\]\)", STORE_SYNC.read_text(encoding="utf-8"))
    names = set(re.findall(r'"([^"]+)"', kept.group(1)))
    from plexora import paths
    from plexora.telemetry import queue

    for name in (paths.REMOTE_CACHE_DIRNAME, paths.IMS_INDEX_DIRNAME, paths.MODELS_DIRNAME, queue.DIRNAME):
        assert name in names, name
    assert "server_token.json" in names
    probe = re.search(r'const PROBE_PREFIX = "([^"]+)"', STORE_SYNC.read_text(encoding="utf-8")).group(1)
    assert probe == paths._PROBE_PREFIX
