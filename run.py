import os
import sys
import multiprocessing

multiprocessing.freeze_support()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    from waitress import serve
    from plexora import app

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

    print(f"Serving on {host}:{port} or http://localhost:{port}")
    serve(
        app,
        host=host,
        port=port,
        max_request_body_size=1073741824000000,
        max_request_header_size=85899345920000,
        threads=8,
    )
