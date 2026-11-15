"""Figure Builder client fixes, each run through a node:vm probe in tests/js.

- figure_adopt_refused_probe.mjs: a refused preview move after adopting
  captures (`{ok: false}`, not a throw) is reported -- the captures are still
  in the bin and could be added twice.
- figure_capture_keys_hidden_probe.mjs: the bare C and S capture keys stand
  down while a routed page (the Figure Canvas, Settings) is over the viewer.
- figure_clipboard_scope_probe.mjs: the object clipboard belongs to the figure
  it was filled in; another figure cannot paste panels naming foreign sources.
- figure_export_waits_for_saves_probe.mjs: Export waits for saves in flight and
  refuses while an edit is not stored.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBES = (
    "figure_adopt_refused_probe.mjs",
    "figure_capture_keys_hidden_probe.mjs",
    "figure_clipboard_scope_probe.mjs",
    "figure_export_waits_for_saves_probe.mjs",
)


@pytest.mark.parametrize("probe", PROBES)
def test_probe(probe):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(REPO_ROOT / "tests" / "js" / probe)], capture_output=True,
                          text=True, encoding="utf-8", cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
