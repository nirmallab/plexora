"""Every child process Plexora starts is started without a console flash.

`popen_kwargs()` is the one place that decides; the static scan below is what
keeps a new spawn site from forgetting to ask it.
"""

import re
from pathlib import Path

from plexora import _subprocess

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "plexora"

#: `subprocess.run(`, `subprocess.Popen(`, `_popen(` and friends.
SPAWN = re.compile(r"\b(?:subprocess\.(?:run|Popen|call|check_call|check_output)|_popen)\(")


def test_a_terminal_or_a_posix_process_changes_nothing():
    assert _subprocess.popen_kwargs(os_name="posix", has_console=False) == {}
    assert _subprocess.popen_kwargs(os_name="nt", has_console=True) == {}


def test_a_console_less_windows_process_hides_its_children():
    assert _subprocess.popen_kwargs(os_name="nt", has_console=False) == {
        "creationflags": 0x08000000}


def _call_text(source, start):
    """The text of the call that opens at `start`, to its closing paren."""
    depth = 0
    for index in range(start, len(source)):
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    return source[start:]


def test_every_spawn_site_in_the_package_asks_for_the_flags():
    missing = []
    for path in PACKAGE.rglob("*.py"):
        parts = path.relative_to(PACKAGE).parts
        if "tests" in parts or path.name == "_subprocess.py":
            continue
        source = path.read_text(encoding="utf-8")
        for match in SPAWN.finditer(source):
            line_start = source.rfind("\n", 0, match.start()) + 1
            if source[line_start:match.start()].lstrip().startswith("#"):
                continue
            call = _call_text(source, match.end() - 1)
            if "popen_kwargs()" not in call and "_spawn_flags()" not in call:
                line = source.count("\n", 0, match.start()) + 1
                missing.append(f"{path.relative_to(ROOT)}:{line}")
    assert missing == [], "spawn without popen_kwargs(): " + ", ".join(missing)
