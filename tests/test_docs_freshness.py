"""The committed reference pages are what the generator produces now.

Runs the real `python tools/docs/sync_docs.py check` in a subprocess (it
dumps the API model in a grandchild with an isolated data root), so this is
exactly what CI runs. On failure the report names each stale page and the
symbol behind it; fix with `python tools/docs/sync_docs.py generate`.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_generated_reference_is_up_to_date(tmp_path):
    from tools.docs.config import child_env

    env = child_env(tmp_path)
    (tmp_path / "home").mkdir(exist_ok=True)
    proc = subprocess.run([sys.executable, "tools/docs/sync_docs.py", "check"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
