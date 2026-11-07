"""The website's telemetry in a real Chrome (tests/browser/telemetry.mjs).

A static build naming a stand-in telemetry service
(tests/browser/mock_telemetry.py, with the Worker's CORS) starts the
desktop's telemetry client in the website's worker: it registers and posts
from the site's origin through the page's fetch, preflighted for the token
and the gzip; the client block says `web`; the install id and the queue are
kept in the site's IndexedDB, so a second visit is the same install and does
not register again; and Global Privacy Control turns it off.

Needs what tests/test_browser_ai.py needs (system Chrome, node,
playwright-core, a website bundle built from this checkout under
PLEXORA_BENCH_HOME); skipped, saying which, without them.
"""

from __future__ import annotations

import json

from tests.test_browser_ai import _drive, _prerequisites


def test_the_website_sends_the_desktops_counts_under_the_same_rules():
    _prerequisites()
    done, out = _drive("telemetry.mjs", timeout=900)
    assert done.returncode == 0 and out.get("ok"), json.dumps(out, indent=1)[-4000:] + done.stderr[-2000:]
    assert [c["name"] for c in out["checks"]] == [
        "a build naming the service starts telemetry, in the default mode, keeping the queue",
        "the first upload registers and sends from the site's origin",
        "the upload is preflighted for the token and the gzip (what the Worker's CORS admits)",
        "the client block says web",
        "the batch is gzipped, carries the token and some counts",
        "a hidden tab's turn queues and keeps what was counted",
        "the install id and the queue are kept in the site's IndexedDB",
        "a second visit is the same install and does not register again",
        "Global Privacy Control turns it off and nothing is sent",
    ]
