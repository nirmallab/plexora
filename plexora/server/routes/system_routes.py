# Process lifecycle routes: File > Quit, and the liveness probe.
#
# Quit used to be `os._exit(0)`, on the grounds that waitress's serve() loop
# never returns on its own. It does: a KeyboardInterrupt on the main thread is
# waitress's clean stop, and plexora._lifetime.request_shutdown raises exactly
# that, the way Ctrl+C would. The difference matters because os._exit skipped
# every atexit handler -- ssh tunnels left running, the remote-store index not
# flushed, a stale nodes.json -- which Ctrl+C in a terminal always ran. A 10 s
# backstop still hard-exits if that teardown wedges.
from plexora import app
from flask import Response, jsonify
import threading

from plexora import _lifetime

#: How long Quit waits before stopping, so this response gets out first: the
#: page shows "Plexora has stopped" only if it heard the 204.
SHUTDOWN_DELAY = 0.25


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
    timer = threading.Timer(SHUTDOWN_DELAY, _lifetime.request_shutdown,
                            args=("Quit was chosen in the page",))
    timer.daemon = True
    timer.start()
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
