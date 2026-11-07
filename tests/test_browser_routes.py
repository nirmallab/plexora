"""Every route of the full app has someone on the website to answer it.

plexora/browser/routes.py decides, per endpoint, whether the page answers it
(``js``), Plexora's Python in the page's worker does (``python``), the site
serves it as a built page or a file (``static``, ``network``), or it is
something a web page cannot do and says so (``handoff``). These fail when a
route is added without that decision, when a decision names a route that is
gone, and when the table the page reads is stale.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from plexora.browser import routes

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def full_app(tmp_path_factory):
    """The full app's endpoints and generated table, from a fresh interpreter
    with every plugin (a test process may have built a narrower one)."""
    env = {**os.environ, "PLEXORA_DATA_PATH": str(tmp_path_factory.mktemp("routes"))}
    env.pop("PLEXORA_PLUGINS", None)
    code = ("import json, plexora; from plexora.browser import routes; app = plexora.app; "
            "print(json.dumps({'endpoints': sorted({r.endpoint for r in app.url_map.iter_rules()}), "
            "'js': routes.generate_js(app)}))")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_every_endpoint_has_a_class(full_app):
    missing = [endpoint for endpoint in full_app["endpoints"] if routes.classify(endpoint) is None]
    assert not missing, (f"no class for {missing}: decide who answers each on the website "
                         "in plexora/browser/routes.py")


def test_no_class_names_an_endpoint_that_is_gone(full_app):
    stale = sorted(routes.named() - set(full_app["endpoints"]))
    assert not stale, f"plexora/browser/routes.py names endpoints that no longer exist: {stale}"


def test_an_endpoint_has_one_class():
    lists = [routes.JS, routes.STATIC, routes.NETWORK, tuple(routes.HANDOFF), routes.PYTHON]
    seen = {}
    for listed in lists:
        for endpoint in listed:
            assert endpoint not in seen, f"{endpoint} is classified twice"
            seen[endpoint] = True


def test_every_handoff_says_why_and_what_to_do():
    for endpoint, (to, why, actions) in routes.HANDOFF.items():
        assert to in ("desktop", "node", "none"), endpoint
        assert why.endswith(".") and len(why) > 30, endpoint
        assert all(action in ("open_desktop", "get_desktop", "pair_node") for action in actions)
        for word in ("upgrade", "unlock", "pro "):
            assert word not in why.lower(), (endpoint, word)


def test_the_page_answers_only_what_it_must():
    """A second implementation of a Python route, each one: the list grows
    only by a decision, with conformance vectors for the new entry. The
    licence's nine (plan D8: the page holds the licence) are held by
    tests/test_license_page.py and tests/js/license_client_probe.mjs."""
    assert len(routes.JS) == 46


def test_a_fixed_route_wins_over_a_variable_one(full_app):
    rows = _rows(full_app)
    first = {}
    for index, row in enumerate(rows):
        first.setdefault(row["endpoint"], index)
    assert first["serve_config"] < first["image_viewer"]


def test_the_patterns_match_what_flask_matches(full_app):
    rows = _rows(full_app)

    def owner(path, method="GET"):
        for row in rows:
            if method in row["methods"] and re.match(row["pattern"].replace("\\/", "/"), path):
                return row["endpoint"]
        return None

    assert owner("/config") == "serve_config"
    assert owner("/lsp11385") == "image_viewer"
    assert owner("/generated/data/lsp/DAPI/0/3_4.png") == "generate_png"
    assert owner("/client/src/js/main.js") == "serveClient"
    assert owner("/plugins/gating/static/gating.js") == "gating.static"
    assert owner("/picker_prefs", "POST") == "save_picker_prefs"
    assert owner("/settings/remotes/o2/connect", "POST") == "settings_remotes_connect"


def test_the_generated_table_is_current(full_app):
    current = routes.GENERATED.read_bytes().decode("utf-8") if routes.GENERATED.exists() else ""
    assert current == full_app["js"], (
        "client/src/js/services/backendRoutes.js is stale: run "
        "`python -m plexora.browser.routes --write`")


def _rows(full_app):
    text = full_app["js"]
    body = text[text.index("Object.freeze([") + len("Object.freeze("):text.rindex("]);") + 1]
    return json.loads(body)
