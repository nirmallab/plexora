"""Methods that call a plugin's routes must reach the network -- and must not
live in core.

The regression this pins: `saveGatingList` kept `lassos: lassos` in its request
payload after `lassos` stopped being one of its parameters. Reading an
undeclared name throws ReferenceError, and the method's own
`catch { console.log(...) }` swallowed it -- so every gate the user set failed
to persist while the UI reported success.

No existing check could see it. `node --check` validates syntax only, the rest
of this suite never executes client JS, and the swallowing catch meant even
calling the method raised nothing. The probe therefore asserts that invoking a
method produces an outbound request, which is the one thing every method here
exists to do.

The second half of this file is the boundary those methods crossed on the way
out of core: `plugins/gating/save_gating_list` and eight sibling URLs were
written into core's dataLayer.js, so a core-only build shipped the addresses of
a tool it does not have, and gating got a client no third-party plugin would be
given.

The probe is a standalone script so it can also be run by hand while editing:

    node tests/js/datalayer_globals_probe.mjs
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "datalayer_globals_probe.mjs"
CORE_JS = REPO_ROOT / "plexora" / "client" / "src" / "js"
PLUGINS_DIR = REPO_ROOT / "plexora" / "plugins"

#: A request to a plugin's namespaced route, e.g. "plugins/gating/upload_gates"
#: or "plugins/roi/api/export.geojson". The tail allows further path segments
#: and a dotted suffix: this used to stop at the first segment, so a plugin that
#: grouped its routes under a prefix -- which is an ordinary thing to do, and
#: what ROI does -- matched nothing, and both guards below silently stopped
#: covering it.
PLUGIN_ROUTE = re.compile(
    r"""["']plugins/([a-z][a-z0-9_]*)/([a-z0-9_]+(?:[/.][a-z0-9_]+)*)["']"""
)


@pytest.fixture(scope="module")
def probe_report():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    # The probe reports on stderr so its own diagnostics never mix with output.
    try:
        return json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def test_every_plugin_route_method_sends_its_request(probe_report):
    failures = [f for source in probe_report for f in source["failures"]]
    assert not failures, (
        "methods that never reached the network: "
        + json.dumps(failures, indent=2)
        + "\nA method here exists to make one request; sending none means it died "
        "on the way, and these methods swallow their own exceptions."
    )


def test_the_probe_is_actually_looking_at_something(probe_report):
    """A probe that checks nothing passes everything."""
    checked = [m for source in probe_report for m in source["checked"]]
    assert len(checked) >= 9
    assert "saveGatingList" in checked


# --------------------------------------------------------------------------
# The boundary: core must not know any plugin's routes
# --------------------------------------------------------------------------

def _js_files(root):
    return sorted(p for p in root.rglob("*.js") if "dist" not in p.parts)


def test_core_javascript_calls_no_plugin_routes():
    """Core shipped nine of gating's URLs on its DataLayer. A core-only build
    carried them for a tool it does not have, and no third-party plugin could
    be given the same treatment -- it has to bring its own client."""
    offenders = {}
    for path in _js_files(CORE_JS):
        hits = sorted({f"plugins/{a}/{b}" for a, b in PLUGIN_ROUTE.findall(path.read_text(encoding="utf-8"))})
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits

    assert not offenders, (
        f"core JavaScript calls plugin routes: {json.dumps(offenders, indent=2)}\n"
        "Move those calls into the plugin that owns the routes."
    )


def test_the_boundary_check_can_actually_fail():
    """The regression as it stood: this is the line core used to hold."""
    line = "await fetch(plexoraUrl('plugins/gating/save_gating_list'), {"
    assert PLUGIN_ROUTE.findall(line) == [("gating", "save_gating_list")]


def test_the_boundary_check_sees_routes_under_a_prefix():
    """A plugin grouping its routes under one -- `/plugins/roi/api/...` -- must
    not fall out of both guards above by being spelled with a second slash."""
    line = "await fetch(this.url('plugins/roi/api/export.geojson'))"
    assert PLUGIN_ROUTE.findall(line) == [("roi", "api/export.geojson")]
    assert _requests_a_plugin_route(line)


def _requests_a_plugin_route(text):
    """A route we fetch or submit ourselves, as opposed to one handed to a
    library. csvGatingList.js passes `plugins/gating/upload_gates` to Dropzone
    as configuration -- Dropzone issues that request, so the probe has nothing
    to invoke and no way to observe it."""
    return any(
        PLUGIN_ROUTE.search(line) and ("fetch(" in line or ".action" in line)
        for line in text.splitlines()
    )


def test_the_probe_covers_every_file_that_requests_a_plugin_route():
    """The probe names its sources explicitly, because each must be loadable in
    isolation. This keeps that list honest: a plugin file that starts issuing
    requests without being added to SOURCES goes unchecked otherwise."""
    covered = {source["file"] for source in json.loads(_probe_sources())}
    requesting = set()
    for plugin_dir in sorted(p for p in PLUGINS_DIR.iterdir() if (p / "__init__.py").exists()):
        for path in _js_files(plugin_dir / "static"):
            if _requests_a_plugin_route(path.read_text(encoding="utf-8")):
                # as_posix, because the probe's SOURCES are written with forward
                # slashes (they are JS paths). str() here gave backslashes on
                # Windows, so nothing ever matched and this guard reported every
                # covered file as uncovered -- failing loudly, but for the wrong
                # reason, and telling the reader to add entries that are already
                # there.
                requesting.add(path.relative_to(REPO_ROOT).as_posix())

    assert requesting <= covered, (
        f"these request plugin routes but the probe never exercises them: "
        f"{sorted(requesting - covered)}. Add them to SOURCES in {PROBE.name}."
    )


def test_the_coverage_check_distinguishes_a_call_from_a_config_value():
    assert _requests_a_plugin_route("let r = await fetch(this.url('plugins/gating/upload_gates'))")
    assert not _requests_a_plugin_route("new Dropzone('#x', { url: this.ctx.url('plugins/gating/upload_gates') })")


def _probe_sources():
    """SOURCES as written in the probe, read rather than imported (it is ESM)."""
    text = PROBE.read_text(encoding="utf-8")
    body = text.split("const SOURCES = ", 1)[1].split(";", 1)[0]
    # JS object literal -> JSON: quote the keys, drop the trailing comma.
    body = re.sub(r"(\w+):", r'"\1":', body)
    body = re.sub(r",(\s*[\]}])", r"\1", body)
    return body
