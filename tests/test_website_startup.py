"""The website's start: the home page before Python, Python's downloads first.

Every page of the website is drawn by Plexora's Python, which takes seconds to
start in the page's worker (Pyodide, its packages, Plexora's wheels, the app),
so nothing of the app showed until it had: 7.0-7.7 s on a first visit and
5.4-6.2 s on every visit after it, measured on the preview in headless Chrome
on the Mac. Now (client services/shell.js, workers/pythonWorker.js):

- the worker is started, and its downloads begun (`warm`), as soon as the
  loader has the bundle's address and has checked the site's switches and the
  browser: 0.27 s into a first visit instead of 1.3 s; the loader's other
  services load together, not one after another;
- the home page this browser was last shown is kept (`pageStore`, Cache
  Storage, per site version, bundle and base path) and shown at once on the
  next visit (`earlyPage`): the frame at 0.14 s, the page usable at 0.6-2.3 s,
  with the status chip saying "Starting" until Python is up. A first visit,
  with nothing kept, shows the home page the site was built with
  (client/static_build.py `home_page`, shell.json `home`; the same bytes the
  website's Python draws for a first visit): usable at 0.9 s and Browse
  answering at 1.0 s, the image open at 7.3-7.5 s (headless Chrome on the
  Mac's GPU; with software GL the first visit's frame draws 5 frames in 4 s,
  a test machine's artefact). Its requests wait
  until then (services/backend.js `PlexoraShellGate`). Python's own answer
  then replaces it unless it is the same (its `<time>` words written in) or
  the page has been used -- built in a second frame nobody sees and faded in
  over it once drawn (`replaceFrame`), not by navigating the frame, which
  showed the loader and drew the page again: a visible reload a few seconds
  into the visit. Picking a file on it before Python is up opens the
  image as soon as Python is, and the kept page already lists the project in
  Recent on the next visit (the home page is kept again after another page).
- only the home page: one that saves what it shows, shown from an old copy,
  could save old values back.

The pieces are checked in node (tests/js/shell_pages_probe.mjs,
tests/js/backend_shim_probe.mjs); the order of the start here.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "plexora" / "client" / "src" / "js"
SHELL = (JS / "services" / "shell.js").read_text(encoding="utf-8")
WORKER = (JS / "workers" / "pythonWorker.js").read_text(encoding="utf-8")
CLIENT = (JS / "services" / "pythonClient.js").read_text(encoding="utf-8")
PROBE = ROOT / "tests" / "js" / "shell_pages_probe.mjs"


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    depth, at = 0, source.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(source[at], 0)
        if depth == 0:
            return source[start:at + 1]
        at += 1


def test_kept_pages_in_node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert "shell pages probe OK" in done.stdout


def test_python_downloads_start_before_the_loaders_other_services():
    boot = _function(SHELL, "hostBoot")
    warm = boot.index("PlexoraPython.warm(")
    # After the browser check, which may stop the site...
    assert boot.index("PlexoraPython.unsupported()") < warm
    # ...and before the services are waited for, the store's question and
    # the boot. The services are fetched from the start, beside it.
    for later in ("await services;", "PlexoraStore.prepare(", "PlexoraPython.start("):
        assert warm < boot.index(later), later
    assert boot.index('services/backend.js"') < boot.index("await (await global.fetch(")
    # The services are fetched together, not one after another.
    assert "await script(" not in boot


def test_the_switches_do_not_hold_up_the_start_but_still_stop_python():
    boot = _function(SHELL, "hostBoot")
    # Read beside Python's downloads, not before them...
    assert boot.index("const flagsRead = readFlags(shell);") < boot.index("PlexoraPython.warm(")
    assert "await readFlags(" not in boot
    halted = boot[boot.index("const halted = flagsRead.then("):boot.index("const pages = pageStore(")]
    assert 'flags.viewer === "desktop-only"' in halted and "flags.min_site_version" in halted
    # ...and a stop is applied before Python starts or the frame is made.
    halt = boot.index("if (halt) return halt();")
    assert halt < boot.index("PlexoraPython.start(") < boot.index('doc.createElement("iframe")')
    # The worker's registration is not waited for: the frame claims itself.
    assert "await control" not in boot
    assert "await claimed()" in _function(SHELL, "frameBoot")


def test_the_frame_is_shown_early_only_with_a_kept_page():
    boot = _function(SHELL, "hostBoot")
    assert "const early = await kept;" in boot
    branch = boot[boot.index("const early = await kept;"):boot.index('doc.createElement("iframe")')]
    assert "PlexoraShellEarly" in branch and "await started" in branch
    # A page asked for in the frame waits for the licence and picked files.
    started = boot[boot.index("const started = (async"):boot.index("started.catch(")]
    for step in ("startLicence(", "PlexoraStore.booted(", "PlexoraPicks.restore(", "PlexoraPyramid.restore("):
        assert step in started, step
    assert re.search(r"const EARLY_ROUTES = new Set\(\[\"/\"\]\);", SHELL)


def test_a_kept_page_holds_its_requests_and_yields_to_python():
    frame = _function(SHELL, "frameBoot")
    assert "global.PlexoraShellGate = host.PlexoraShellStarted" in frame
    early = _function(SHELL, "earlyPage")
    assert 'PlexoraStatus.begin("Starting")' in early
    # Replaced only when unused, and never from a page already left.
    assert early.index("global.document !== doc") < early.index("if (!used) {")
    # Replaced by the host, by a frame built unseen and faded in over it --
    # never by navigating the frame, which took the page down, showed the
    # loader and drew it again: a visible reload a few seconds into a visit.
    replacing = early[early.index("if (!used) {"):early.index('title: "This project changed"')]
    assert "host.PlexoraShellReplace" in replacing
    assert "location.replace" not in replacing and "location.reload" not in replacing
    # Watched for use until the host has decided, a zoom included.
    assert '"wheel"' in early
    assert early.index("unwatched();", early.index("if (!used) {")) > early.index("host.PlexoraShellReplace")


def test_a_kept_page_is_replaced_by_a_frame_built_unseen():
    """Python's page for a kept one is written in a second frame over the
    first, invisible and taking no pointer, and faded in once it has drawn
    (`drawnIn`: its load, fonts, a viewer's image, its entrance played out);
    the kept page is dropped after the fade. Nothing is timed to the
    browser: it waits for this browser's Python and page, and the limits only
    bound a page that never says it is done. Used meanwhile, the kept page
    stays and the second frame goes."""
    boot = _function(SHELL, "hostBoot")
    assert "global.PlexoraShellReplace = (address, used) => replaceFrame(address, used, keeps);" in boot
    assert boot.index("global.PlexoraShellReplace") < boot.index("doc.body.appendChild(frame)")
    replace = _function(SHELL, "replaceFrame")
    assert '"plexora-app-frame is-standby"' in replace and "old.after(frame)" in replace
    assert "await drawnIn(frame.contentWindow, used)" in replace
    # Used first, or never written: the kept page stays.
    used = replace.index("if (used() || what === \"late\")")
    assert replace.index("frame.remove();", used) < replace.index('frame.id = "plexora-app"')
    # The app's id moves to the new frame; the old one goes after the fade.
    assert replace.index('old.removeAttribute("id")') < replace.index('frame.id = "plexora-app"')
    assert "old.remove(), VEIL_MS" in replace
    drawn = _function(SHELL, "drawnIn")
    assert 'readyState === "complete"' in drawn and "fonts.ready" in drawn and "imageDrawn(win)" in drawn
    assert "settled(win)" in drawn
    # The new frame's page says when it is written, or that it must be seen.
    assert "standby.written()" in SHELL and "standby.needed()" in SHELL
    assert "shared.shown(global)" in _function(SHELL, "reveal")
    assert "shared.standing(global)" in _function(SHELL, "unveil")
    css = (ROOT / "plexora/client/src/css/shell.css").read_text(encoding="utf-8")
    standby = css[css.index(".plexora-app-frame.is-standby"):]
    assert "opacity: 0" in standby[:standby.index("}")] and "pointer-events: none" in standby[:standby.index("}")]


def test_the_worker_warms_outside_its_queue_and_boot_takes_over():
    handler = WORKER[WORKER.index("self.onmessage = "):]
    assert handler.index('data.op === "warm"') < handler.index("queue.push(data)")
    warm = _function(WORKER, "warm")
    # Nothing in warm may build the app (its environment is boot's) or let a
    # step run (steps start once `py` is set).
    assert "py =" not in warm.replace("pyodide =", "")
    assert "runPythonAsync" not in warm and "pyimport" not in warm
    # The first boot (`boot` hands a page of the same tab what it made).
    boot = _function(WORKER, "firstBoot")
    assert boot.index("await warm(args)") < boot.index("py = warmed.pyodide")
    assert "firstBoot(args)" in _function(WORKER, "boot")
    assert "warm," in CLIENT and "function warm(" in CLIENT


def test_the_start_asks_nothing_about_storage():
    """The first visit no longer asks where projects go once Python has started
    (`askWhereOnce`, removed 2026-11-09), and a later visit no longer asks in
    the loader for a folder the browser stopped granting: the store is
    prepared without a question, and the first save runs the onboarding
    (views/webOnboarding.js). A frame's load (the first one's, or one that
    replaces a kept page's: `keeps`) does nothing but keep the home page."""
    boot = _function(SHELL, "hostBoot")
    assert "await global.PlexoraStore.prepare().catch(() => null);" in boot
    assert "askWhereOnce" not in SHELL and "keepInBrowser" not in SHELL
    load = boot[boot.index('const keeps = (one) => one.addEventListener("load"'):]
    load = load[:load.index("});")]
    assert "keepHome(one, pages)" in load and "PlexoraStore" not in load
    assert "keeps(frame);" in boot
    # The store is prepared before Python starts, which it hands the folder to.
    assert boot.index("PlexoraStore.prepare(") < boot.index("PlexoraPython.start(")
