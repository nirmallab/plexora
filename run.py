import os
import sys
import multiprocessing

multiprocessing.freeze_support()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    # Before waitress, whose serve() adds a handler only to a root that has none.
    from plexora import _logging
    _logging.configure()

    from waitress import serve
    from plexora import app
    from plexora._resources import WAITRESS_LIMITS

    # use port 8000 if no port is specified via command line argument
    port = 8000 if len(sys.argv) < 2 or not str.isdigit(sys.argv[1]) else sys.argv[1]

    def str2bool(v):
        return v.lower() in ("yes", "true", "t", "1")

    # Only ever turned ON here. create_app() has already set it from
    # PLEXORA_DOCKER, which is how the container flags itself now, and writing
    # an unconditional False back would undo that -- the image's CMD passes no
    # positional arguments at all.
    if len(sys.argv) > 2 and str2bool(sys.argv[2]):
        app.config["IS_DOCKER"] = True

    # Only call freeze_support if we're in a frozen environment

    # Loopback by default. This used to bind 0.0.0.0, which put a server with
    # no authentication of any kind on every interface of the machine -- fine
    # inside a container, which is why the Docker image passes a host
    # explicitly, and not fine on a workstation or an HPC login node.
    host = os.environ.get("PLEXORA_HOST", "127.0.0.1")
    # Bound to loopback, the viewer answers only loopback's own names, which
    # is what stops a rebound DNS name reaching it (see plexora._guard). A
    # container binds 0.0.0.0 and is reached by whatever name it is given.
    if host in ("127.0.0.1", "localhost", "::1") and app.config.get("PLEXORA_TRUSTED_HOSTS") is None:
        app.config["PLEXORA_TRUSTED_HOSTS"] = []

    from plexora import cli

    cli.start_telemetry(app, "other")
    print(f"Serving on {host}:{port} or http://localhost:{port}")
    serve(
        app,
        host=host,
        port=port,
        **WAITRESS_LIMITS,
        threads=8,
    )
