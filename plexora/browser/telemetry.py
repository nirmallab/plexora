"""Telemetry on the website: the desktop's, in a worker that has no threads.

The website sends the same anonymous counts the desktop app sends, under the
same rules and with the same code -- plexora/telemetry's schema, queue,
batches and uploader. What differs is where things are kept and who keeps
time:

- **Consent** is `config.resolve`'s, unchanged: `DO_NOT_TRACK` (the page sets
  it when the browser sends Do Not Track or Global Privacy Control, as a
  shell's environment would), the mode chosen in Settings > Usage data, the
  server's pause and ceiling, else anonymous. The one-time notice is the
  desktop's toast (services/telemetry.js), answered by the same routes.
- **Where it is kept**: `home` (/telemetry), which the worker keeps in the
  site's IndexedDB (`plexora-telemetry`, workers/pythonWorker.js) -- never in
  the projects folder, which the desktop app may share. `prefs.json` is the
  settings block (the mode, whether the notice was shown, and the install id:
  a random id of this browser's, never the licence's), and `queue.sqlite` the
  queue, in the one tab that holds the queue's Web Lock. Another tab counts
  into a queue in memory and sends what it counted itself, open hour
  included, when it is hidden: two copies of one queue would send its rows
  twice.
- **Time**: no threads (`Telemetry.pump`). The worker calls `tick` a minute
  after the start, then every few minutes and when its tab is hidden: the
  aggregate into the queue every time, and an upload attempt once the
  uploader's interval has passed (an hour, or what the server says), under
  the uploader's own backoff.
- **The network**: the uploader posts through urllib, which is the page's
  fetch here (plexora/browser/fetch.py), to the Worker, which admits the
  site's origin by exact match (backend/src/cors.ts).

The client block says `runtime: web` (and `launch_mode`, `deployment` and
`install_kind` `web`), so the website's numbers can be set beside the
desktop's per size band.
"""

from __future__ import annotations

import time
from pathlib import Path

PREFS = "prefs.json"
QUEUE = "queue.sqlite"

#: When the last upload attempt was (monotonic seconds), or None.
_last_upload: float | None = None
#: Whether this tab's queue is the one kept across visits.
_kept = False


def start(home: str = "/telemetry", *, keep_queue: bool = True, scratch: str | None = None,
          context: dict | None = None) -> dict:
    """Telemetry for this tab, from what the worker has read back into
    `home`. `keep_queue`: this tab holds the queue's lock, and its queue is
    `home`'s; otherwise one of its own in `scratch` -- by default a new
    folder for this start (the worker's memory on the website), so no queue
    left by anything before is ever sent again. `context`: what the browser
    reports (`os` family, `cpus`, `memory_gb`). Returns
    `{enabled, mode, source, kept}`."""
    global _kept, _last_upload
    import tempfile

    import plexora
    from plexora.telemetry import config
    from plexora.telemetry.client import telemetry
    from plexora.telemetry.queue import Queue

    _kept = bool(keep_queue)
    _last_upload = None
    config.set_prefs_file(Path(home) / PREFS)
    if telemetry._queue is None:
        if keep_queue:
            where = Path(home)
        else:
            where = Path(scratch) if scratch else Path(tempfile.mkdtemp(prefix="plexora-telemetry-"))
        telemetry._queue = Queue(where / QUEUE)
    telemetry.context = {**telemetry.context, **(context or {})}
    telemetry.start(plexora.app, serve_mode="web", upload=False)
    return state()


def state() -> dict:
    from plexora.telemetry.client import telemetry

    return {"enabled": bool(telemetry.enabled), "mode": telemetry.mode,
            "source": telemetry.resolved.source, "kept": _kept}


def tick(hidden: bool = False, now: float | None = None) -> dict:
    """The writer's and uploader's turn: the aggregate into the queue, and an
    upload attempt when the uploader's interval has passed since the last
    one (the first tick always tries). `hidden`: the tab is being hidden,
    perhaps closed -- a tab whose queue is not kept sends its open hour too,
    since nothing will send it later. `{outcome, enabled, mode, kept}`."""
    global _last_upload
    from plexora.telemetry import uploader
    from plexora.telemetry.client import telemetry

    now = time.monotonic() if now is None else now
    due = _last_upload is None or now - _last_upload >= uploader.interval(telemetry)
    if hidden and not _kept:
        due = True
    outcome = telemetry.pump(upload=due, include_open=hidden and not _kept)
    if due and outcome not in ("off", "flushed"):
        _last_upload = now
    return {"outcome": outcome, **state()}


def tick_json(hidden: bool = False) -> str:
    import json

    return json.dumps(tick(bool(hidden)))


def start_json(options: str) -> str:
    """`start` from the worker: `options` is JSON
    `{home, keep_queue, context}`."""
    import json

    given = json.loads(options or "{}")
    return json.dumps(start(given.get("home") or "/telemetry",
                            keep_queue=bool(given.get("keep_queue", True)),
                            context=given.get("context") or {}))
