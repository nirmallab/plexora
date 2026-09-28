"""`plexora telemetry status|on|diagnostics|off|reset|preview|send|sample`."""

from __future__ import annotations

import argparse
import datetime as _dt
import json

ACTIONS = ("status", "on", "anonymous", "diagnostics", "off", "reset", "preview", "send",
           "sample")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="plexora telemetry",
        description="Show or change what anonymous usage data Plexora sends. "
                    "Nothing is ever sent about your data, files, names or machine.")
    parser.add_argument("action", nargs="?", default="status", choices=ACTIONS,
                        help="status (default); on/anonymous, diagnostics or off; reset "
                             "(new install id); preview (print the next upload); send "
                             "(upload now); sample (print a synthetic batch)")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    parser.add_argument("--fixture", action="store_true",
                        help="With `sample`: write backend/test/fixtures/sample-batch.json.")
    return parser


def _when(value):
    if not value:
        return "never"
    try:
        return _dt.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "unknown"


_SOURCES = {
    "testing": "the test suite pins it off",
    "do_not_track": "DO_NOT_TRACK is set",
    "env": "PLEXORA_TELEMETRY is set",
    "server": "the telemetry service has paused this version",
    "ceiling": "the telemetry service caps this version",
    "settings": "your setting",
    "default": "the default",
}


def _print_status(status, log):
    log(f"Telemetry: {status['mode']} ({_SOURCES.get(status['source'], status['source'])})")
    if status.get("install_id"):
        log(f"Install id: {status['install_id']} (random; `plexora telemetry reset` replaces it)")
    pending = status.get("pending") or {}
    if pending:
        log(f"Queued on this machine: {pending.get('counter_rows', 0)} counter rows, "
            f"{pending.get('records', 0)} records, {pending.get('batches_waiting', 0)} "
            f"batches waiting")
    uploader = status.get("uploader") or {}
    if not status.get("endpoint_configured"):
        log("Uploads: no telemetry service is configured, so nothing leaves this machine.")
    else:
        log(f"Last upload: {_when(uploader.get('last_upload'))}; last attempt: "
            f"{uploader.get('last_outcome') or 'none'}")
        if uploader.get("backoff_until"):
            log(f"Next attempt not before {_when(uploader['backoff_until'])} "
                f"(the service did not answer).")
    log("Off: `plexora telemetry off`, PLEXORA_TELEMETRY=off, or DO_NOT_TRACK=1.")


def run(argv, log=print) -> int:
    args = build_parser().parse_args(argv)
    from plexora.telemetry import report, sample

    if args.action == "sample":
        if args.fixture:
            log(str(sample.write_fixture()))
        else:
            log(json.dumps(sample.body(), indent=2, sort_keys=True))
        return 0
    if args.action in ("on", "anonymous", "diagnostics", "off"):
        mode = "anonymous" if args.action == "on" else args.action
        status = report.set_mode(mode)
        if args.json:
            log(json.dumps(status, indent=2))
        else:
            _print_status(status, log)
            if status["mode"] != mode:
                log(f"(You chose {mode}; {_SOURCES.get(status['source'], status['source'])} "
                    f"overrides it.)")
        return 0
    if args.action == "reset":
        status = report.reset()
        log(json.dumps(status, indent=2) if args.json else
            f"New install id: {status.get('install_id') or '(minted when telemetry next runs)'}")
        return 0
    if args.action == "preview":
        preview = report.preview()
        if not args.json and not preview["would_send"]:
            log("# Nothing would be sent: "
                + ("telemetry is off." if preview["mode"] == "off"
                   else "no telemetry service is configured."))
        log(json.dumps(preview["body"], indent=2, sort_keys=True))
        return 0
    if args.action == "send":
        from plexora.telemetry import uploader
        from plexora.telemetry.client import telemetry

        outcome = uploader.upload_once(telemetry, include_open=True, force=True)
        log(json.dumps({"outcome": outcome}) if args.json else f"Upload: {outcome}")
        return 0 if outcome in ("sent", "nothing") else 1
    status = report.status()
    if args.json:
        log(json.dumps(status, indent=2))
    else:
        _print_status(status, log)
    return 0
