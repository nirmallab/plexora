# Process lifecycle routes for the local desktop app (File > Quit).
#
# Plexora has no native window/process the frontend already controls -- it's
# a headless waitress server the user opens in their own browser -- so Quit
# has to be a request that tells this process to terminate itself. os._exit
# skips normal interpreter teardown, which is fine here: waitress's serve()
# loop never returns on its own, so there's no clean in-process shutdown path
# to call instead, and the user closing the terminal/window today has the
# same abrupt effect.
from plexora import app
from flask import Response, jsonify
import os


@app.route('/shutdown', methods=['POST'])
def shutdown():
    # In a notebook the server is a sidecar the kernel owns: it was started by
    # PlexoraViewer, it is tracked in that module's registry, and atexit is
    # what stops it. os._exit here would kill it behind the kernel's back,
    # leaving a viewer object whose iframe silently stops loading and no way to
    # get it back short of restarting the kernel. Under a hosted proxy it is
    # worse still -- the "process" the button would end is one the hub spawned.
    if app.config.get('PLEXORA_NOTEBOOK_MODE'):
        return jsonify(error="Shutdown is managed by the notebook session."), 403
    os._exit(0)
    return Response(status=204)


@app.route('/health', methods=['GET'])
def health():
    """Liveness probe for the navbar status indicator (appStatus.js).

    Deliberately does no work -- not even touching the loaded datasource -- so
    the 10 s poll can never contend with tile serving. It answers exactly one
    question: is this process still accepting requests? An idle page issues no
    other requests, so without this a server that died minutes ago would still
    show as connected.
    """
    return Response(status=204)
