"""The desktop app's "Open on the Website" (plan: "Website <-> desktop: one
library", item 4).

A project kept in the library the website shares -- Documents/Plexora/projects
(`paths.website_library`, or `PLEXORA_LIBRARY_PATH`'s folders) -- opens on the
website at `links.WEBSITE_URL` + its name: in a Chromium that has that folder
granted the project is already there, and its images come with one folder
grant. A project anywhere else is not one the website can see, and the menu
says so in one line instead of opening a page that would not find it. Moving
it there is not offered: a project's folder holds its derived files under the
root it was made in, and nothing moves one project between roots safely yet.

**Hidden until the website's GA** (plan decision 3: what the desktop shows of
the website ships at GA, dark before). The switch is the one the GA cut-over
already flips: the website's own flags (`links.WEBSITE_FLAGS_URL`, set with
`tools/browser_build/publish.py flags --viewer on`). The row shows only while
they say `viewer: "on"`: before GA they say "preview", a paused website says
"desktop-only" (the kill switch hides the row too, within `CACHE_SECONDS`),
and unreachable or malformed flags -- offline, a firewall -- hide it. No
desktop release is needed at GA, and installers built before it light up then.
The read is a plain GET of a static file, with nothing about the user in it,
at most once per `CACHE_SECONDS`, and only when the desktop window opens a
project page.

Only the desktop app's server offers it (`PLEXORA_DESKTOP`): a browser tab of
a terminal `plexora` is already a web page, and the website is itself.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.parse import quote

#: How long the flags are trusted before they are read again.
CACHE_SECONDS = 600
#: How long the read may take; past it, the row stays hidden.
TIMEOUT_SECONDS = 3

#: Where the website's library is, as the user sees it.
LIBRARY_LABEL = "Documents › Plexora"

_lock = threading.Lock()
_cached: tuple[float, dict] | None = None


def _fetch_flags() -> dict:
    from urllib.request import Request, urlopen

    from plexora import links

    request = Request(links.WEBSITE_FLAGS_URL, headers={"Accept": "application/json"})
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 -- a fixed https address
        document = json.loads(response.read(64 * 1024).decode("utf-8"))
    return document if isinstance(document, dict) else {}


def flags(fetch=None) -> dict:
    """The website's flags, read at most once per CACHE_SECONDS; {} when they
    cannot be read. `fetch` stands in for the read (tests)."""
    global _cached
    with _lock:
        if _cached is not None and time.monotonic() - _cached[0] < CACHE_SECONDS:
            return dict(_cached[1])
    try:
        found = (fetch or _fetch_flags)()
        found = found if isinstance(found, dict) else {}
    except Exception:  # noqa: BLE001 -- offline, refused, malformed: hidden, never an error
        found = {}
    with _lock:
        _cached = (time.monotonic(), found)
    return dict(found)


def reset() -> None:
    """Forget the flags (tests)."""
    global _cached
    with _lock:
        _cached = None


def website_open(fetch=None) -> bool:
    """Whether the website is open to everyone: the GA switch."""
    return flags(fetch).get("viewer") == "on"


def home_root(name: str) -> Path | None:
    """The root whose registry holds project `name`, the user's own first."""
    from plexora import paths

    for root in paths.roots():
        if name in paths._registered(root):
            return root
    return None


def in_library(name: str) -> bool:
    """Whether project `name` is kept in the library the website shares."""
    from plexora import paths

    root = home_root(name)
    return root is not None and paths.is_website_library(root)


def url_for(name: str) -> str:
    """The project's page on the website."""
    from plexora import links

    return links.WEBSITE_URL + quote(str(name), safe="")


def describe(app, name: str | None, fetch=None) -> dict:
    """What the File menu's row does for project `name`:
    `{shown, in_library, url, library, message}`. `shown` False hides the row
    (not the desktop app's server, no project, or the website not open yet);
    `url` is set only for a project the website can open."""
    from plexora import browser_rendering

    out = {"shown": False, "in_library": False, "url": None, "library": LIBRARY_LABEL,
           "message": None}
    if not name or not browser_rendering.is_desktop(app) or not website_open(fetch):
        return out
    out["shown"] = True
    if in_library(name):
        out.update(in_library=True, url=url_for(name))
    else:
        out["message"] = f"To open it on the website, keep the project in {LIBRARY_LABEL}."
    return out
