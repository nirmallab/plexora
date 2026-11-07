"""The website's Content-Security-Policy (plan Phase 5; client/static_build.py
`CSP`) and what it requires of the pages: no inline script and no inline
event handler in any template the website renders -- the policy runs none, so
one added later would silently do nothing there. tests/test_browser_*.py run
the site under the enforced policy end to end."""

import re
from pathlib import Path

from plexora.client import static_build

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = [*sorted((ROOT / "plexora" / "client" / "templates").glob("*.html")),
             *sorted((ROOT / "plexora" / "plugins").glob("*/templates/**/*.html"))]

#: The one inline script left: base.html's branch for a server's page (the
#: website's branch reads its variables from JSON with services/shellVariables.js).
SERVER_ONLY = {("base.html", 'window.flaskVariables = passVariablesToFrontend({{data|tojson}});')}


def _directives():
    return dict(part.split(" ", 1) for part in static_build.CSP.split("; "))


def test_the_policy_runs_no_inline_or_evaluated_script():
    policy = _directives()
    assert "'unsafe-inline'" not in policy["script-src"] and "'unsafe-eval'" not in policy["script-src"]
    assert "'wasm-unsafe-eval'" in policy["script-src"]
    assert policy["object-src"] == "'none'" and policy["base-uri"] == "'self'"
    # The app is a frame of its own loader: framed by itself, by no one else.
    assert policy["frame-ancestors"] == "'self'"
    # A CSP host source cannot name an IPv6 address; Chrome drops such a source.
    assert "[" not in static_build.CSP


def test_every_page_of_the_site_is_served_under_it():
    everywhere = static_build.HEADERS.split("\n\n")[0]
    assert f"  Content-Security-Policy: {static_build.CSP}" in everywhere.splitlines()


def test_no_template_has_an_inline_script_or_handler():
    found = set()
    for template in TEMPLATES:
        text = template.read_text(encoding="utf-8")
        text = re.sub(r"{#.*?#}", "", text, flags=re.S)  # Jinja comments
        for match in re.finditer(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", text, flags=re.S):
            attrs, body = match.group("attrs"), match.group("body").strip()
            if "src=" in attrs or 'type="application/json"' in attrs or not body:
                continue
            first = next(line.strip() for line in body.splitlines() if line.strip() and "//" not in line[:4])
            found.add((template.name, first))
        for handler in re.finditer(r"\son[a-z]+\s*=\s*[\"']", text):
            found.add((template.name, handler.group(0).strip()))
    assert found == SERVER_ONLY
