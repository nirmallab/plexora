"""ctx.layers.onLayerChange: every subscription is released at plugin
teardown, not only the last (main.js pluginLayerApi;
tests/js/plugin_layer_unsubscribe_probe.mjs). Skipped without node."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "plugin_layer_unsubscribe_probe.mjs"


def test_teardown_releases_every_layer_subscription():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "all checks passed" in proc.stdout, out
    assert proc.stdout.count("ok - ") == 4, out
