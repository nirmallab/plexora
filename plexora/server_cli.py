import argparse
import os


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run Plexora as a notebook-friendly sidecar server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default="8000")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--notebook-mode", action="store_true")
    parser.add_argument("--plugins", default=None,
                        help="Comma-separated plugins to activate. Omit for all installed; pass an empty string for a core-only build.")
    args = parser.parse_args(argv)

    if args.data_dir:
        os.environ["PLEXORA_DATA_PATH"] = args.data_dir
    if args.base_url is not None:
        os.environ["PLEXORA_BASE_URL"] = args.base_url
    if args.notebook_mode:
        os.environ["PLEXORA_NOTEBOOK_MODE"] = "1"
    if args.plugins is not None:
        os.environ["PLEXORA_PLUGINS"] = args.plugins

    from waitress import serve
    from plexora import app, _clean_base_url
    from plexora._resources import worker_threads

    app.config["PLEXORA_NOTEBOOK_MODE"] = args.notebook_mode or app.config.get("PLEXORA_NOTEBOOK_MODE", False)
    if args.base_url is not None:
        app.config["PLEXORA_BASE_URL"] = _clean_base_url(args.base_url)
    # Plugin registration (Blueprint mounting) already happened inside
    # create_app() at the `from plexora import app` line above, keyed off the
    # PLEXORA_PLUGINS env var set above -- unlike PLEXORA_BASE_URL and
    # PLEXORA_NOTEBOOK_MODE, there is no post-import app.config override that
    # could retroactively register a Blueprint.
    print(f"Serving Plexora on {args.host}:{args.port}")
    serve(
        app,
        host=args.host,
        port=int(args.port),
        max_request_body_size=1073741824000000,
        max_request_header_size=85899345920000,
        # Sized from the allocation rather than hardcoded: most workers block
        # on the serialized image reader rather than computing, so the pool is
        # deliberately wider than the core count -- what matters is that one
        # stays free to answer /health while the rest wait.
        threads=worker_threads(),
    )


if __name__ == "__main__":
    main()
