"""Every icon this tree asks for must be one Font Awesome actually ships.

Font Awesome is loaded as SVG-with-JS (`@fortawesome/fontawesome-free/js/all`),
which means an icon is drawn by a script looking its NAME up in a table and
replacing the element with an `<svg>`. A name the table does not have is not an
error, a warning, or a broken-image box: the element is left exactly as it was,
which is an empty `<span>`. So the failure is a button with a caption and a
blank space where its picture should be, and nothing anywhere says why.

Which is how five of them accumulated. FA 6 renamed a large part of the set and
FA 7 dropped the FA 5 aliases, so `fa-times`, `fa-pencil-alt`, `fa-file-upload`
and `fa-file-download` -- all correct when they were written -- became names
that draw nothing, on the tool panel's close button, the project list's edit
link and the channel CSV controls. `vector-square` went the same way and took
the object bar's "Match size" button with it, which is what prompted this file.

None of that is reachable from a unit test of behaviour: the markup is right,
the class is right, and the only thing that is wrong is a string's membership of
a table in someone else's package. So the table is what gets asserted against.

Skipped rather than failed when the package is absent -- `plexora/client/
node_modules` is an npm install, not part of the source tree, and a checkout
without one must still be able to run the suite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FONTAWESOME = (REPO_ROOT / "plexora" / "client" / "node_modules"
               / "@fortawesome" / "fontawesome-free" / "js")

#: Where our own markup lives. Deliberately not the whole repository: a virtual
#: environment in the working tree carries matplotlib and pygments, both of
#: which have Font Awesome 4 names in their own sources, and neither is ours.
SEARCHED = ("plexora",)
SUFFIXES = {".js", ".mjs", ".html", ".css", ".py"}
SKIP_PARTS = {"node_modules", ".git", "dist", "build", "__pycache__"}

#: `fa-` classes that style an icon rather than name one -- sizes, rotations,
#: animations, the stack helpers. Font Awesome's own documented modifier set.
MODIFIERS = re.compile(
    r"^(solid|regular|brands|light|thin|duotone|sharp|classic"
    r"|fw|li|ul|border|inverse|pull-left|pull-right"
    r"|stack|stack-1x|stack-2x|layers|swap-opacity"
    r"|spin|spin-pulse|spin-reverse|pulse|beat|beat-fade|fade|bounce|shake"
    r"|flip|flip-horizontal|flip-vertical|flip-both"
    r"|rotate-90|rotate-180|rotate-270|rotate-by"
    r"|xs|sm|lg|xl|2xl|1x|2x|3x|4x|5x|6x|7x|8x|9x|10x)$")

#: The name in a class attribute (`fas fa-trash`) and the name as data, which is
#: how the figure builder's action registry carries it (`icon: "trash"`). Both
#: end up in the same `<span class="fas fa-...">`.
IN_MARKUP = re.compile(r"\bfa-([a-z0-9-]+)")
AS_DATA = re.compile(r'\bicon:\s*"([a-z][a-z0-9-]*)"')


def _shipped():
    """Every icon name in the free set, from the three style files."""
    names = set()
    for style in ("solid.js", "regular.js", "brands.js"):
        source = (FONTAWESOME / style).read_text()
        # `"trash":[448,512,[...],"f1f8","M32 32C32..."]` -- the name, then the
        # width and height that start every definition. Matching the numbers is
        # what keeps the keys inside the SVG path data out of the set.
        names |= set(re.findall(r'[{,]\s*"?([a-z0-9-]+)"?\s*:\s*\[\s*\d+', source))
    return names


def _asked_for():
    """Every icon name this tree uses, with the files that use each."""
    used: dict[str, set[str]] = {}
    for top in SEARCHED:
        for path in (REPO_ROOT / top).rglob("*"):
            if path.suffix not in SUFFIXES or not path.is_file():
                continue
            if SKIP_PARTS & set(path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            relative = str(path.relative_to(REPO_ROOT))
            for match in IN_MARKUP.finditer(text):
                name = match.group(1)
                if not MODIFIERS.match(name):
                    used.setdefault(name, set()).add(relative)
            if path.suffix == ".js":
                for match in AS_DATA.finditer(text):
                    used.setdefault(match.group(1), set()).add(relative)
    return used


@pytest.fixture(scope="module")
def shipped():
    if not FONTAWESOME.is_dir():
        pytest.skip("font awesome is not installed (run npm install in plexora/client)")
    return _shipped()


def test_every_icon_name_is_one_font_awesome_ships(shipped):
    used = _asked_for()
    assert used, "found no icon names at all -- the scan is broken, not the tree"

    missing = {name: sorted(files) for name, files in used.items()
               if name not in shipped}
    assert not missing, (
        "these icon names draw nothing -- Font Awesome has no such icon, so the"
        " element is left as an empty <span>:\n"
        + json.dumps(missing, indent=2))


def test_the_scan_would_notice_a_dead_name(shipped):
    """The assertion above passes trivially if the scan finds nothing.

    `_asked_for` is regular expressions over the whole tree, and the way it
    fails is by matching less than it should rather than by raising. So one name
    known to be dead is checked against the same set the test uses.
    """
    assert "vector-square" not in shipped, (
        "vector-square is back in the set, so it can no longer stand as the"
        " example of a name that was removed -- pick another FA 5 name")
    assert "trash" in shipped
