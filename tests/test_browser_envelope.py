"""The website's notices of what a tab does well, drawn by the real page in a
real Chrome (tests/browser/envelope.mjs): a long batch, magic select's model
with no WebGPU adapter, and slow tiles -- each a short title, one line and
every button on one row inside the card, once per project per tab, with
"Continue here" and the x remembered -- and the host's tile watcher seeing
the frame's tile requests. The rules themselves: tests/test_envelope.py.

Needs what tests/test_browser_ai.py needs (system Chrome, node,
playwright-core, a website bundle under PLEXORA_BENCH_HOME); skipped, saying
which, without them.
"""

from __future__ import annotations

import json

from tests.test_browser_ai import _drive, _prerequisites


def test_the_notices_on_the_website():
    _prerequisites()
    done, out = _drive("envelope.mjs", timeout=600)
    assert done.returncode == 0 and out.get("ok"), json.dumps(out, indent=1)[-4000:] + done.stderr[-2000:]
    assert [c["name"] for c in out["checks"]] == [
        "the host's watcher sees the frame's tile requests",
        "a long batch: title, one line, three buttons on one row",
        "Continue here is remembered for the project",
        "said once per project per tab",
        "magic select without WebGPU: the measured second a click, buttons on one row",
        "slow tiles: a node beside the data, two buttons on one row",
        "the x is remembered too",
    ]
