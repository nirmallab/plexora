"""A long request on the website is waited for, not cut off.

The service worker (client src/js/workers/backendRelay.js) relays each route
request to the page and used to give up after 120 s whatever the page was
doing, so importing a flat image whose pyramid takes longer than that to
build (a 38.4 GB one: 136-144 s here) answered 504 and the project never
opened. The page now says it is still at work every 30 s
(services/backend.js) and the relay only gives up after 120 s of silence; and
the import itself, which can wait on a build longer than the 5 minutes Chrome
lets a service worker hold a fetch, is sent through PlexoraBackend.fetch,
which the website answers from the page's own Python. tests/browser/places.mjs
imported the 38.4 GB image this way (docs/internal/bench/envelope/cells/).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "plexora" / "client" / "src" / "js"


def _const(source: str, name: str) -> int:
    return int(re.search(rf"const {name} = (\d+);", source).group(1))


def test_the_page_says_it_is_at_work_well_inside_the_relays_limit():
    relay = (JS / "workers" / "backendRelay.js").read_text(encoding="utf-8")
    backend = (JS / "services" / "backend.js").read_text(encoding="utf-8")
    assert _const(backend, "WORKING_MS") * 3 <= _const(relay, "TIMEOUT_MS")
    assert "port.postMessage({ working: true })" in backend
    assert "clearInterval(working)" in backend.split("port.postMessage({ working: true })", 1)[1]
    # The relay re-arms its timer on each and resolves only on the answer.
    assert re.search(r"if \(data && data\.working\) \{ arm\(\); return; \}", relay)


def test_the_import_does_not_go_through_the_service_worker():
    for name in ("views/importSample.js", "views/quickViewLanding.js"):
        source = (JS / name).read_text(encoding="utf-8")
        sent = source.count('plexoraUrl("import/sample")')
        direct = source.count('(window.PlexoraBackend?.fetch || fetch)(plexoraUrl("import/sample")')
        assert sent and direct == sent, (name, sent, direct)
