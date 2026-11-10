"""The website's first-use onboarding: where the user's work is kept, asked at
the first action that keeps something, never on load.

The dialog itself (views/webOnboarding.js) and the pick's gate it hangs on
(services/pickedFiles.js) are checked in node by tests/js/web_onboarding_probe.mjs,
against a small DOM and a stand-in for the host page's PlexoraStore; this
wrapper runs it and pins its lines. The rest here is wiring that only the
files themselves can show: the page never asks on load any more (services/
shell.js), the script is the website's only, a pick passes the gate, a new
server waits for it, and the dialog's CSS is where the app shell keeps it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT = REPO_ROOT / "plexora" / "client"
JS = CLIENT / "src" / "js"
PROBE = REPO_ROOT / "tests" / "js" / "web_onboarding_probe.mjs"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than silently reducing what is covered.
CHECKS = (
    "ensure is ready at once when a place is chosen and open, and draws nothing",
    "ensure is ready at once on a page with no host store",
    "the first use opens on the welcome screen: the name large, its version, and the two cards",
    "the welcome screen says what Plexora does, in four points, each with its own icon",
    "the first screen says the setup is asked once, in this browser",
    "the header names Plexora and the site's version, with no Lite badge on any screen",
    "the X closes it as a cancel, and the dialog is taken down",
    "two calls while it is open share one dialog",
    "new, folder: Create your Plexora folder, the folder made the workspace, and Continue to my image",
    "the folder dialog is the first thing the click awaits",
    "returning: a workspace found shows its projects, and Continue adopts it as it is",
    "the library an old version made inside is found, and nothing is moved",
    "a folder that is not a workspace: choose another, set it up, or start in the browser",
    "a cancelled folder dialog says so, and Choose folder opens it again",
    "a refused permission shows the denied screen with Try again",
    "no folder picker: the folder option is replaced by why, and Locate explains it",
    "browser storage asks the browser to keep it from the click, then says it is ready",
    "no room in the browser shows the quota screen",
    "a folder that cannot be written, and one only partly set up, each say so; Try again runs it again",
    "a folder that already has Plexora data, or other files, is confirmed before it is used",
    "a workspace outside Documents › Plexora says the desktop app needs it added",
    "a lapsed folder opens on Reconnect; Reconnect asks from the click and resolves ready",
    "Continue with browser storage sets the folder aside for this visit and goes on",
    "a refused reconnect shows the denied screen; a folder that is gone offers another",
    "Back returns to the screen before, and the first screen has none",
    "while work is under way the X and Escape do nothing",
    "closed after the workspace is ready, the work still goes on",
    "the progress dots follow the steps, and all are done at the end",
    "each screen labels the dialog and takes the focus to its title",
    "a little confetti when ready, and none under reduced motion",
    "motion: the head icon draws on, the step pill slides, an error shakes its tile",
    "names from the user's disk are text, never markup",
    "a pick's gate runs before anything is mounted or recorded, and a no leaves nothing",
)


def _read(*parts):
    return Path(*parts).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run([node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=120)


def test_the_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert line in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert f"{len(CHECKS)} checks passed" in probe.stdout


def test_the_page_never_asks_where_on_load():
    """The two-button question shell.js asked once Python had started, and
    the loader's "Open folder" question, are gone: nothing is asked until
    something is kept, and a lapsed folder is reconnected from a click."""
    shell = _read(JS, "services", "shell.js")
    for gone in ("askWhereOnce", "keepInBrowser", "Keep in browser", "Use browser this time"):
        assert gone not in shell, gone
    assert "PlexoraStore.prepare()" in shell
    # What pairing and the version still need stays.
    assert "async function whenApp(frame)" in shell
    assert "global.PlexoraShellStarted = started;" in shell
    assert "global.PlexoraSite = Object.freeze({ version:" in shell
    store = _read(JS, "services", "browserStore.js")
    assert "shouldAsk" not in store and "shouldWarn" not in store
    assert "chooseHome" not in store


def test_the_script_is_the_websites_only_and_after_the_dialogs_it_uses():
    base = _read(CLIENT, "templates", "base.html")
    at = base.index("views/webOnboarding.js")
    gate = base.rindex("{% if data.backend == 'browser' %}", 0, at)
    assert "{% endif %}" not in base[gate:at]
    assert base.index("views/confirmDialog.js") < at
    assert base.index("services/toast.js") < at
    assert 'webOnboarding.js?v=' in base and "defer" in base[at:base.index(">", at)]


def test_a_website_pick_passes_the_gate():
    """Both ways a website Browse picks -- the explorer over the folders the
    user gave the site, and a browser's own dialog -- carry the gate, and so
    does a drop (attachWebDrop, importSample.js)."""
    browse = _read(JS, "services", "browsePicker.js")
    questions = browse[browse.index("function pickQuestions()"):browse.index("function pressedButton()")]
    assert 'ensure({ reason: "pick" })' in questions
    start = browse.index("async function browseForPath(")
    branch = browse[start:browse.index("// A page without the explorer", start)]
    assert "const {ask, gate} = pickQuestions();" in branch
    assert "local.take(chosen, {ask, gate})" in branch
    assert "local.dialog({mode, filter, accept, many, ask, gate," in branch
    assert "mode: kind, multiple: many, ask, gate, types: pickerTypes(filter)," in branch
    assert "picks.take(event.dataTransfer, pickQuestions())" in browse
    dialog = _read(JS, "views", "importSample.js")
    assert "typeof pickQuestions === \"function\" ? pickQuestions() : {}" in dialog


def test_the_gate_comes_before_every_mount_and_record():
    picks = _read(JS, "services", "pickedFiles.js")
    pick = picks[picks.index("async function pick("):picks.index("async function sha256(")]
    # Three branches: a folder by the dialog, a folder by the input, files.
    starts = [pick.index("if (global.showDirectoryPicker)"), pick.index("const files = await chooseWithInput({ directory: true });"),
              pick.index("let handles = null;")]
    for start, end in zip(starts, starts[1:] + [len(pick)]):
        branch = pick[start:end]
        gate = branch.index("await allowed()")
        for later in ("mountTree(", "mountFiles(", "mountSlide(", "remember(", "slideFolder("):
            if later in branch:
                assert gate < branch.index(later), later


def test_a_new_server_waits_for_a_place_on_the_website():
    modal = _read(JS, "services", "connectionModal.js")
    submit = modal[modal.index("async function submitRecipe("):modal.index("const answer = await saveRecipe(")]
    assert 'ensure({ reason: "remote" })' in submit and "!editing" in submit and "webCloud()" in submit


def test_the_dialog_css_is_in_main_css_and_guards_hidden():
    css = _read(CLIENT, "src", "css", "main.css")
    assert "Start of the onboarding block" in css
    block = css[css.index("Start of the onboarding block"):css.index("End of the onboarding block")]
    assert ".plx-onb [hidden]" in block and "display: none !important" in block
    assert "prefers-reduced-motion: reduce" in block
    assert "--duration-base" in block
    # The Lite palette, and no emoji glyphs anywhere in the dialog.
    assert "--plx-lite-cyan" in block and "--plx-lite-violet" in block
    # One type scale for every screen, named once at the top of the block.
    for name in ("--plx-onb-name-size", "--plx-onb-title-size", "--plx-onb-lead-size",
                 "--plx-onb-small-size", "--plx-onb-eyebrow-size"):
        assert name + ":" in block, name
    source = _read(JS, "views", "webOnboarding.js")
    assert not any(ord(ch) > 0x2FFF for ch in source), "an emoji or pictograph in the dialog"


def test_desktop_handoff_says_when_the_desktop_app_needs_the_folder_added():
    handoff = _read(JS, "services", "desktopHandoff.js")
    assert "desktopFinds === false" in handoff
    assert "Settings › Data › Locations" in handoff


def _rules(css):
    """(selectors, declarations, inside a reduced-motion @media) for every
    rule in `css`, @media blocks opened one level; keyframes skipped."""
    out = []
    at, depth, media = 0, 0, False
    while True:
        open_ = css.find("{", at)
        if open_ < 0:
            return out
        head = css[at:open_].strip()
        # Comments before a rule are not part of its selector.
        while "/*" in head:
            head = head[:head.index("/*")] + head[head.index("*/") + 2:]
        head = head.strip()
        if head.startswith("@media"):
            media = "prefers-reduced-motion: reduce" in head
            depth += 1
            at = open_ + 1
            continue
        close = css.find("}", open_)
        if head.startswith("@keyframes"):
            # Its frames are rules inside it: skip to its own closing brace.
            level, at = 1, open_ + 1
            while level:
                nxt_open, nxt_close = css.find("{", at), css.find("}", at)
                if 0 <= nxt_open < nxt_close:
                    level, at = level + 1, nxt_open + 1
                else:
                    level, at = level - 1, nxt_close + 1
        else:
            out.append(([part.strip() for part in head.split(",")], css[open_ + 1:close], media and depth > 0))
            at = close + 1
        # A media block closes with a lone brace after its last rule.
        rest = css[at:].lstrip()
        while depth and rest.startswith("}"):
            depth -= 1
            media = False
            at = len(css) - len(rest) + 1
            rest = css[at:].lstrip()


def test_every_animation_in_the_dialog_stops_under_reduced_motion():
    """Each selector in the onboarding block that runs an animation is named
    again under `prefers-reduced-motion: reduce` with `animation: none`, so
    nothing moves for somebody who asked for that -- whatever its timing."""
    css = _read(CLIENT, "src", "css", "main.css")
    block = css[css.index("Start of the onboarding block"):css.index("End of the onboarding block")]
    block = block[block.index("*/") + 2:]
    rules = _rules(block)
    stopped = {sel for sels, decls, reduced in rules if reduced and "animation: none" in decls for sel in sels}
    animated = {sel for sels, decls, reduced in rules if not reduced
                for line in decls.split(";") if line.strip().startswith("animation:")
                and "none" not in line for sel in sels}
    assert animated, "no animations found: the parser is wrong"
    assert animated <= stopped, sorted(animated - stopped)
    # And only the spinners and the busy rim loop.
    loops = {sel for sels, decls, reduced in rules if not reduced and "infinite" in decls for sel in sels}
    assert loops == {".plx-onb[data-phase=\"working\"]::before",
                     ".plx-onb-task.is-running .plx-onb-task-mark::after",
                     ".plx-onb .plx-button-primary.is-working .plx-onb-button-icon svg"}, loops
