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
from flask import Response
import os


@app.route('/shutdown', methods=['POST'])
def shutdown():
    os._exit(0)
    return Response(status=204)
