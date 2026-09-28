"""The local end of telemetry: what the page asks, and what tabs report.

- `GET /telemetry/status` -- the mode, why, whether the one-time notice is
  due, and how often a tab should post. Every page asks once at load.
- `POST /telemetry/mode` `{"mode": "off"|"anonymous"|"diagnostics"}` -- the
  Settings page's radios. Takes effect at once.
- `POST /telemetry/notice_seen` -- the notice's OK button.
- `POST /telemetry/reset` -- a new install id, nothing queued under the old.
- `GET /telemetry/preview` -- the next upload exactly as it would be sent.
- `POST /telemetry/send` -- one upload attempt now; answers with a word.
- `POST /telemetry/ingest` -- a tab's aggregate (see services/telemetry.js).
  The browser is a producer only: it never talks to the telemetry service,
  so there is one install identity per machine and nothing in the page
  reaches the network.

Nothing here reads a datasource, and everything here answers even when
telemetry is off -- off is a mode, not an error.
"""

from __future__ import annotations

import re
import threading
import time

from flask import jsonify, request

from plexora import app
from plexora.telemetry import report, schema
from plexora.telemetry.client import telemetry
from plexora.telemetry.queue import window_of

MAX_INGEST_BYTES = 64 * 1024
MAX_INGEST_ROWS = 2000
#: A tab posts every five minutes and on hide/unload; one post per ten
#: seconds per tab is far more than it needs and far less than a loop would.
INGEST_MIN_SPACING_S = 10.0

_VIEWER_ID = re.compile(r"[0-9a-f]{16,32}")
_last_ingest: dict = {}
_window_viewers: dict = {}
_ingest_lock = threading.Lock()


def _json_body():
    if not request.is_json:
        return None
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


@app.route('/telemetry/status', methods=['GET'])
def telemetry_status():
    return jsonify(report.status(brief=bool(request.args.get("brief"))))


@app.route('/telemetry/mode', methods=['POST'])
def telemetry_mode():
    body = _json_body()
    if body is None:
        return jsonify(success=False, error="expected a JSON body"), 415
    try:
        return jsonify(report.set_mode(body.get("mode")))
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400


@app.route('/telemetry/notice_seen', methods=['POST'])
def telemetry_notice_seen():
    return jsonify(report.notice_seen())


@app.route('/telemetry/reset', methods=['POST'])
def telemetry_reset():
    if _json_body() is None:
        return jsonify(success=False, error="expected a JSON body"), 415
    return jsonify(report.reset())


@app.route('/telemetry/preview', methods=['GET'])
def telemetry_preview():
    return jsonify(report.preview())


@app.route('/telemetry/send', methods=['POST'])
def telemetry_send():
    if _json_body() is None:
        return jsonify(success=False, error="expected a JSON body"), 415
    from plexora.telemetry import uploader

    telemetry.sync(timeout=2.0)
    outcome = uploader.upload_once(telemetry, include_open=True, force=True)
    return jsonify(outcome=outcome, status=report.status())


def validate_ingest(body):
    """The rows of a tab's post, or None if anything in it is not allowed.

    Whole-body: one unknown key rejects the post, so a tab that has drifted
    from the schema is noticed rather than half-counted.
    """
    if not isinstance(body, dict) or set(body) - {"schema", "viewer_id", "rows"}:
        return None
    if body.get("schema") != schema.SCHEMA_VERSION:
        return None
    viewer = body.get("viewer_id")
    rows = body.get("rows")
    if not isinstance(viewer, str) or not _VIEWER_ID.fullmatch(viewer):
        return None
    if not isinstance(rows, list) or len(rows) > MAX_INGEST_ROWS:
        return None
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        event = row.get("e")
        if event not in schema.BROWSER_EVENTS:
            return None
        pinned = schema.BROWSER_EVENTS[event] or {}
        dims = row.get("d") if isinstance(row.get("d"), dict) else None
        if dims is None or any(dims.get(k) != v for k, v in pinned.items()):
            return None
        upload_row = {k: v for k, v in row.items() if k != "e"}
        if not schema.validate_upload_row(event, upload_row, schema.DIAGNOSTICS):
            return None
        clean.append((event, upload_row))
    return viewer, clean


@app.route('/telemetry/ingest', methods=['POST'])
def telemetry_ingest():
    if not telemetry.enabled:
        return "", 204
    if (request.content_length or 0) > MAX_INGEST_BYTES:
        return jsonify(success=False, error="too large"), 413
    raw = request.get_data(cache=False, as_text=False)
    if len(raw) > MAX_INGEST_BYTES:
        return jsonify(success=False, error="too large"), 413
    import json

    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        body = None
    checked = validate_ingest(body)
    if checked is None:
        telemetry.count("telemetry.health", "rejected", event="render.summary")
        return jsonify(success=False, error="not allowed by the telemetry schema"), 400
    viewer, rows = checked
    now = time.monotonic()
    window = window_of()
    with _ingest_lock:
        last = _last_ingest.get(viewer)
        if last is not None and now - last < INGEST_MIN_SPACING_S:
            return jsonify(success=False, error="too soon"), 429
        _last_ingest[viewer] = now
        if len(_last_ingest) > 4096:
            for key in sorted(_last_ingest, key=_last_ingest.get)[:2048]:
                _last_ingest.pop(key, None)
        seen = _window_viewers.setdefault(window, set())
        for stale in [w for w in _window_viewers if w != window]:
            _window_viewers.pop(stale, None)
        first_in_window = viewer not in seen
        seen.add(viewer)
    telemetry.note_viewer(viewer)
    if first_in_window:
        telemetry.count("render.summary", "viewers")
    fold(rows)
    return "", 204


def fold(rows):
    """Add a tab's rows to this server's current window."""
    for event, row in rows:
        dims = row.get("d") or {}
        if "h" in row:
            telemetry.add_hist(event, row["k"], row["h"], row.get("s") or 0.0,
                               row.get("mx") or 0.0, **dims)
        else:
            telemetry.count(event, row["k"], row["n"], **dims)
            if event == "error.fingerprint":
                with telemetry._lock:
                    telemetry._errors["browser"] += row["n"]


def _reset_for_tests():
    with _ingest_lock:
        _last_ingest.clear()
        _window_viewers.clear()
