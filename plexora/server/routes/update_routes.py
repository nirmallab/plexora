"""Help > Check for Updates, for the pip-installed server.

Four routes, in the order the dialog uses them:

- `GET /update/check` -- what is running, what is published, and whether this
  install can take the newer one in place. `?auto=1` is the page's once-a-day
  background check, answered from the last result without touching the
  network when it is recent; `?force=1` skips every cache.
- `POST /update/install` -- starts pip in a background thread, pinned to the
  version the dialog showed. Refused (409) when anything about that is not
  true any more.
- `GET /update/install/status` -- the job's state and the tail of pip's log.
- `POST /update/restart` -- stops the server the way Quit does and has the
  launcher start it again on the same port; the page polls `/health` for the
  new version and reloads.

Unlike `/shutdown`, restart is allowed in notebook mode: on POSIX the relaunch
is an exec, so the pid the kernel's registry holds for this sidecar is still
the pid of the server that comes back. A server that was not started by one of
Plexora's own launchers has nobody to start it again, and says so rather than
stopping for good.

The desktop app never uses these for installing: its Python is inside a
signed, read-only bundle, and the Tauri shell updates the whole app. `check`
still answers there, with `mode: "desktop"`, for a browser tab opened from the
app.
"""

from __future__ import annotations

import datetime as _dt
import threading

from flask import jsonify, request

from plexora import _lifetime, app, updates

#: Captured now, at server start, rather than at the first request: after pip
#: has run, the metadata on disk names the NEW version while this process is
#: still the old one. See `updates.current_version`.
RUNNING_VERSION = updates.current_version()

#: The background check's spacing. The server enforces it, so any number of
#: tabs opened in a day cost the index one request between them.
AUTO_CHECK_SECONDS = 24 * 3600

#: Same reason as system_routes.SHUTDOWN_DELAY: the 202 has to get out first.
RESTART_DELAY = 0.25


def _mode():
    if app.config.get('PLEXORA_DESKTOP'):
        return 'desktop'
    if app.config.get('PLEXORA_NOTEBOOK_MODE'):
        return 'notebook'
    return 'browser'


def _can_restart():
    """True when a launcher is waiting to start this server again.

    Set by `plexora` (cli.main) and the notebook sidecar just before they
    serve. Anything else -- the desktop server, a WSGI host, a test client --
    would simply stop, which is Quit, not restart.
    """
    return bool(app.config.get('PLEXORA_CAN_RESTART')) and not app.config.get('PLEXORA_DESKTOP')


def _busy():
    """Sentences for work a restart would cut short, or [] when there is none.

    Layer and bin builds and segmentation all report through layer_jobs; a
    record still `pending` with a real stage is a thread doing work. The
    "waiting" stage is a layer nothing here knows how to build, and waits for
    ever -- it is not work in progress.
    """
    try:
        from plexora.server.models import layer_jobs

        with layer_jobs._lock:
            records = list(layer_jobs._jobs.items())
    except Exception:
        return []
    busy = []
    for (sample, _job_id), record in records:
        if record.get('status') == 'pending' and record.get('stage') != 'waiting':
            label = record.get('stage_label') or record.get('message') or 'A build'
            busy.append(f"{label} ({sample})")
    return busy


def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _age_seconds(stamp):
    try:
        then = _dt.datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=_dt.timezone.utc)
    return (_dt.datetime.now(_dt.timezone.utc) - then).total_seconds()


def _job_view():
    job = updates.current_job()
    if job is None:
        return None
    view = job.snapshot()
    view['installed'] = updates.installed_on_disk()
    return view


def _payload(prefs, kind, latest=None, notes='', notes_url=''):
    current = RUNNING_VERSION
    available = bool(latest) and updates.is_newer(latest, current)
    return {
        'current': current or 'source checkout',
        'latest': latest,
        'available': available,
        'skipped': bool(available and prefs.get('skipped_version') == latest),
        'notes': notes,
        'notes_url': notes_url or (updates.RELEASE_URL.format(version=latest) if latest else ''),
        'mode': _mode(),
        'kind': kind['kind'],
        'can_install': bool(kind['can_install']) and available,
        'can_restart': _can_restart(),
        'reason': kind['reason'],
        'command': updates.manual_command(latest, kind) if latest else '',
        'auto_check': prefs['auto_check'],
        'last_checked': prefs.get('last_checked'),
        'busy': _busy(),
        'job': _job_view(),
    }


def _kind():
    return updates.install_kind(desktop=bool(app.config.get('PLEXORA_DESKTOP')))


@app.route('/update/check', methods=['GET'])
def update_check():
    prefs = updates.read_prefs()
    kind = _kind()
    auto = request.args.get('auto') == '1'
    force = request.args.get('force') == '1'

    # The desktop app asks its shell, which reads the signed release manifest
    # rather than PyPI -- the two can disagree for the hours a release takes
    # to build. This only supplies the preferences it shares.
    if kind['kind'] == 'desktop':
        return jsonify(_payload(prefs, kind))

    if auto:
        # Off means off: not even the cached answer, so turning the setting
        # off makes the badge go away rather than lingering until tomorrow.
        if not prefs['auto_check']:
            return jsonify(_payload(prefs, kind) | {'disabled': True})
        age = _age_seconds(prefs.get('last_checked'))
        if age is not None and age < AUTO_CHECK_SECONDS:
            return jsonify(_payload(prefs, kind, prefs.get('last_latest')))

    try:
        found = updates.fetch_latest(force=force)
    except Exception as exc:
        # 200, not 5xx: being offline is an answer the dialog shows, and the
        # background check must stay silent about it rather than log an error.
        body = _payload(prefs, kind)
        body['error'] = (f"Could not reach the package index ({exc.__class__.__name__}). "
                         f"Check the network connection and try again.")
        return jsonify(body)

    try:
        prefs = updates.write_prefs(last_checked=_now_iso(),
                                    last_latest=found.get('latest'))
        prefs['last_latest'] = found.get('latest')
    except Exception:
        pass  # a read-only settings file is no reason to hide the answer
    return jsonify(_payload(prefs, kind, found.get('latest'),
                            found.get('notes') or '', found.get('notes_url') or ''))


@app.route('/update/install', methods=['POST'])
def update_install():
    payload = request.get_json(silent=True) or {}
    version = str(payload.get('version') or '').strip()
    kind = _kind()
    if kind['kind'] == 'desktop':
        return jsonify(error="The desktop app updates itself from its own window."), 409
    if not kind['can_install']:
        return jsonify(error=kind['reason']), 409
    try:
        latest = updates.fetch_latest().get('latest')
    except Exception:
        latest = None
    # Pinned to the answer this server gave, not to whatever the page sends:
    # the button installs what the dialog said it would, and nothing else.
    if not version or version != latest or not updates.is_newer(version, RUNNING_VERSION):
        return jsonify(error=f"{version or 'That version'} is not the update on offer. "
                             f"Check for updates again."), 409
    job = updates.start_install(version, updates.pip_command(version, uv=kind.get('uv')))
    if job is None:
        return jsonify(error="An update is already being installed."), 409
    return jsonify(_job_view()), 202


@app.route('/update/install/status', methods=['GET'])
def update_install_status():
    view = _job_view()
    if view is None:
        return jsonify(state='idle')
    return jsonify(view)


@app.route('/update/restart', methods=['POST'])
def update_restart():
    if not _can_restart():
        return jsonify(error="This server was not started by a Plexora launcher, so "
                             "nothing would start it again. Stop it and start "
                             "Plexora yourself."), 409
    payload = request.get_json(silent=True) or {}
    busy = _busy()
    if busy and not payload.get('force'):
        return jsonify(error="Work in progress would be stopped.", busy=busy), 409
    timer = threading.Timer(RESTART_DELAY, _lifetime.request_restart,
                            args=("restarting to finish an update",))
    timer.daemon = True
    timer.start()
    return jsonify(restarting=True, version=RUNNING_VERSION), 202


@app.route('/update/prefs', methods=['POST'])
def update_prefs():
    payload = request.get_json(silent=True) or {}
    changes = {}
    if 'auto_check' in payload:
        changes['auto_check'] = bool(payload['auto_check'])
    if 'skipped_version' in payload:
        skipped = payload['skipped_version']
        changes['skipped_version'] = str(skipped) if skipped else None
    try:
        prefs = updates.write_prefs(**changes) if changes else updates.read_prefs()
    except OSError as exc:
        return jsonify(error=f"Could not save the setting: {exc}"), 500
    return jsonify(prefs)

