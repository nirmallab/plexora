"""Which apps have a project open now.

A project kept in the folder the website and the desktop app share
(Documents/Plexora/projects) can be open in both at once, and each keeps its
own copy of what it is working on: the two would write over each other's
changes. So each app leaves a mark in the project's folder while the project
is open -- one small file per app, `.open-<app>.json`, refreshed by the
viewer once a minute -- and the viewer says when another app's mark is fresh.
A mark not refreshed for FRESH_SECONDS is an app that closed (or crashed).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from plexora import paths
from plexora.server.models import sources

PREFIX = ".open-"
FRESH_SECONDS = 150


def _where(app: str) -> str:
    if app == "website":
        return "the website"
    host = app.split(":", 1)[1] if app.startswith("host:") else app
    return "the desktop app" if host == sources.app_key().split(":", 1)[-1] else f"Plexora on {host}"


def _mark_path(folder: Path, app: str) -> Path:
    return folder / f"{PREFIX}{re.sub(r'[^A-Za-z0-9_.-]+', '_', app)}.json"


def beat(name: str) -> None:
    """This app has project `name` open: its mark refreshed."""
    folder = paths.project_state_dir(name)
    if not folder.is_dir():
        return
    app = sources.app_key()
    try:
        _mark_path(folder, app).write_text(json.dumps({"app": app, "beat": time.time()}), encoding="utf-8")
    except OSError:
        pass


def others(name: str) -> list:
    """The other apps with project `name` open now: [{app, where, beat}]."""
    folder = paths.project_state_dir(name)
    me = sources.app_key()
    out = []
    try:
        marks = sorted(folder.glob(f"{PREFIX}*.json"))
    except OSError:
        return out
    now = time.time()
    for mark in marks:
        try:
            record = json.loads(mark.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        app = str(record.get("app") or "")
        stamp = float(record.get("beat") or 0)
        if app and app != me and now - stamp < FRESH_SECONDS:
            out.append({"app": app, "where": _where(app), "beat": stamp})
    return out
