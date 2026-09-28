"""`plexora license ...` -- see, install, activate and release a licence.

    plexora license                        status (the default)
    plexora license status [--json]
    plexora license activate KEY [--name NAME] [--cluster]
    plexora license install FILE|CERTIFICATE
    plexora license refresh
    plexora license deactivate
    plexora license remove [--forget-environment]
    plexora license fingerprint [--name NAME] [--cluster] [--out FILE] [--json]
    plexora license environment show|register [--name NAME] [--cluster]
    plexora license lease [--ttl 48h] [--entitlement ENT]
    plexora license trial [--email ADDRESS]

Nothing here is needed to use Plexora. Free works with no licence at all, and
every command that talks to the licence service says so before it does.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys

ACTIONS = ("status", "activate", "install", "refresh", "deactivate", "remove",
           "fingerprint", "environment", "lease", "trial")

_STATE_WORDS = {
    "free": "Free",
    "trial": "Paid (trial)",
    "paid_active": "Paid",
    "offline_valid": "Paid (offline licence)",
    "grace": "Paid (expired -- in grace period)",
    "expired": "Free (Paid licence expired)",
    "revoked": "Free (Paid licence revoked)",
    "invalid": "Free (licence could not be verified)",
}

_REASON_WORDS = {
    "no_license": "no licence is installed",
    "environment_mismatch": "the certificate was issued for a different environment",
    "unknown_key": "the certificate was signed with a key this Plexora does not trust",
    "bad_signature": "the certificate has been altered",
    "malformed": "the licence file is not a certificate",
    "wrong_product": "the certificate is not for Plexora",
    "unsupported_version": "the certificate needs a newer Plexora",
    "future_dated": "the certificate is dated in the future (check the clock)",
    "unreadable_file": "PLEXORA_LICENSE_FILE could not be read",
    "token_not_exchanged": "PLEXORA_LICENSE_TOKEN has not been exchanged for a certificate",
    "offline_refused": "PLEXORA_LICENSE_OFFLINE forbids exchanging the token",
    "unknown_plan": "the certificate names an unknown plan",
}

_SOURCE_WORDS = {
    "cache": "installed on this machine",
    "token": "PLEXORA_LICENSE_TOKEN",
    "file": "PLEXORA_LICENSE_FILE",
    "job": "PLEXORA_LICENSE_JOB_CERT (a job licence)",
    "none": "none",
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="plexora license",
        description="Show or manage this machine's Plexora licence. Free needs no "
                    "licence; Paid unlocks AI features.")
    sub = parser.add_subparsers(dest="action")

    status = sub.add_parser("status", help="Show the plan and licence state (default).")
    status.add_argument("--json", action="store_true")

    activate = sub.add_parser("activate", help="Activate a seat key or licence token online.")
    activate.add_argument("credential", help="PLEX-XXXX-XXXX-XXXX-XXXX or PLXT1_...")
    activate.add_argument("--name", help="A name for this environment in the portal.")
    activate.add_argument("--cluster", action="store_true",
                          help="Register this as a whole HPC cluster (run on a login node).")
    activate.add_argument("--json", action="store_true")

    install = sub.add_parser("install", help="Install an offline licence file or certificate.")
    install.add_argument("source", help="A .plexora file, or a PLEXORA1 certificate string.")
    install.add_argument("--json", action="store_true")

    refresh = sub.add_parser("refresh", help="Check the licence with the service now.")
    refresh.add_argument("--json", action="store_true")

    deactivate = sub.add_parser("deactivate",
                                help="Release this environment from its seat, then remove "
                                     "the local licence.")
    deactivate.add_argument("--json", action="store_true")

    remove = sub.add_parser("remove", help="Remove the local licence (no network).")
    remove.add_argument("--forget-environment", action="store_true",
                        help="Also discard this environment's identity. The next "
                             "activation registers a new environment.")

    fingerprint = sub.add_parser("fingerprint",
                                 help="Print what the portal's offline-licence form needs.")
    fingerprint.add_argument("--name")
    fingerprint.add_argument("--cluster", action="store_true")
    fingerprint.add_argument("--out", help="Write the report to this file.")
    fingerprint.add_argument("--json", action="store_true")

    env = sub.add_parser("environment", help="Show or register this environment.")
    env.add_argument("what", choices=("show", "register"))
    env.add_argument("--name")
    env.add_argument("--cluster", action="store_true")
    env.add_argument("--credential", help="Seat key or token (default: PLEXORA_LICENSE_TOKEN).")
    env.add_argument("--json", action="store_true")

    lease = sub.add_parser("lease",
                           help="Mint a short job licence from a registered cluster, for "
                                "PLEXORA_LICENSE_JOB_CERT in a job that cannot see $HOME.")
    lease.add_argument("--ttl", default="48h", help="Lifetime, e.g. 12h or 3d (max 7d).")
    lease.add_argument("--entitlement", action="append",
                       help="Narrow the job to this entitlement (repeatable).")
    lease.add_argument("--server", action="store_true",
                       help="Ask the licence service to sign it instead of minting locally.")

    trial = sub.add_parser("trial", help="Start a 30-day Paid trial.")
    trial.add_argument("--email", help="Send the trial key here directly instead of "
                                       "opening the portal.")
    return parser


def _when(value) -> str:
    if not value:
        return "never"
    try:
        return _dt.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "unknown"


def _validity_lines(info: dict) -> list[str]:
    """The licence's end date, and a renewal date only when one needs acting on.

    The certificate's own (90-day) end is renewed online unseen, so it is
    never shown as the licence's end.
    """
    validity = info.get("validity") or {}
    lines = []
    until, ended = validity.get("until"), validity.get("ended")
    if until:
        env = info.get("environment") or {}
        if info.get("trial"):
            label = "Trial ended" if ended else "Trial ends"
        elif env.get("type") == "job":
            label = "Job licence ended" if ended else "Job licence ends"
        else:
            label = "Ended" if ended else "Valid until"
        lines.append(f"{label}: {_when(until)}")
    if ended and info["state"] == "grace" and info.get("grace_until"):
        lines.append(f"Paid features stop: {_when(info['grace_until'])}")
    renew, renew_by = validity.get("renew"), validity.get("renew_by")
    if renew == "file":
        lines.append(f"Offline licence file works until: {_when(renew_by)}; download a new one "
                     "from the licence portal before then." if renew_by else
                     "This offline licence file has run out; download a new one from the "
                     "licence portal.")
    elif renew == "online":
        lines.append(f"Renew by: {_when(renew_by)}. Connect to the internet and Plexora renews "
                     "it automatically, or run `plexora license refresh`." if renew_by else
                     "Paid features are paused until this licence is renewed: connect to the "
                     "internet and run `plexora license refresh`.")
    return lines


def status_lines(info: dict) -> list[str]:
    lines = [f"Plan: {_STATE_WORDS.get(info['state'], info['state'])}"]
    if info["state"] == "free" and info.get("reason") == "no_license":
        lines.append("Everything Free works with no licence. Paid unlocks AI features: "
                     "`plexora license trial`, or `plexora license activate <key>`.")
        return lines
    reason = info.get("reason")
    if info["state"] in ("invalid",) or reason in _REASON_WORDS and info["state"] == "free":
        lines.append(f"Why: {_REASON_WORDS.get(reason, reason)}")
    if info.get("license_id"):
        lines.append(f"Licence: {info['license_id']}"
                     + (f" ({info['use_class']})" if info.get("use_class") else ""))
    if info.get("entitlements"):
        lines.append("Unlocks: " + ", ".join(info["entitlements"]))
    env = info.get("environment") or {}
    if env.get("type"):
        name = f"{env['name']} " if env.get("name") else ""
        lines.append(f"Environment: {name}({env['type']})")
    lines.extend(_validity_lines(info))
    if info.get("source") and info["source"] != "none":
        lines.append(f"From: {_SOURCE_WORDS.get(info['source'], info['source'])}")
    if info["source"] in ("cache", "token") and info["state"] != "invalid":
        lines.append(f"Last checked with the licence service: {_when(info.get('last_validated'))}")
    if info.get("clock_rollback"):
        lines.append("This machine's clock is behind a time Plexora has already seen; "
                     "`plexora license refresh` re-checks it online.")
    if info["state"] in ("expired", "revoked"):
        lines.append("Gates, ROIs, figures and everything else you made are untouched; "
                     "only Paid features are off.")
    return lines


def _print(obj, as_json, log, lines=None):
    if as_json:
        log(json.dumps(obj, indent=2, sort_keys=True))
    else:
        for line in (lines if lines is not None else [str(obj)]):
            log(line)


def _parse_ttl(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([hd]?)\s*", text or "")
    if not match:
        raise SystemExit(f"--ttl: {text!r} is not a duration like 12h or 3d")
    n, unit = int(match.group(1)), match.group(2) or "h"
    return n * (86400 if unit == "d" else 3600)


def run(argv, log=print) -> int:
    parser = build_parser()
    args = parser.parse_args(argv or ["status"])
    action = args.action or "status"

    from plexora.licensing import state as license_state
    from plexora.licensing.errors import LicenseError

    try:
        if action == "status":
            info = license_state.reload().describe(now=license_state.now())
            _print(info, args.json, log, status_lines(info))
            return 0
        if action == "install":
            return _install(args, log)
        if action == "activate":
            return _activate(args.credential, args, log)
        if action == "environment":
            if args.what == "show":
                return _environment_show(args, log)
            from plexora.licensing import store

            credential = args.credential or store.env_token()
            if not credential:
                log("Give a seat key or licence token with --credential, or set "
                    "PLEXORA_LICENSE_TOKEN.")
                return 2
            return _activate(credential, args, log)
        if action == "refresh":
            return _refresh(args, log)
        if action == "deactivate":
            return _deactivate(args, log)
        if action == "remove":
            return _remove(args, log)
        if action == "fingerprint":
            return _fingerprint(args, log)
        if action == "lease":
            return _lease(args, log)
        if action == "trial":
            return _trial(args, log)
    except LicenseError as exc:
        log(str(exc))
        return 1
    parser.print_help()
    return 2


def _install(args, log) -> int:
    from pathlib import Path

    from plexora.licensing import certificate, state as license_state, store

    source = args.source.strip()
    if certificate.looks_like(source):
        cert = source
    else:
        path = Path(source).expanduser()
        try:
            cert = store.read_license_file(path)
        except (OSError, ValueError) as exc:
            log(f"Could not read a licence from {path}: {exc}")
            return 1
    installed = license_state.install_certificate(cert)
    info = installed.describe(now=license_state.now())
    _print(info, args.json, log, ["Installed.", *status_lines(info)])
    return 0 if installed.paid else 1


def _activate(credential, args, log) -> int:
    from plexora.licensing import client, state as license_state

    kind = "cluster" if getattr(args, "cluster", False) else None
    result = client.activate(credential, kind=kind, name=getattr(args, "name", None))
    license_state.save_activation(result, credential=credential, source="activation")
    resolved = license_state.reload(network=False)
    info = resolved.describe(now=license_state.now())
    lines = ["Activated.", *status_lines(info)]
    if (info.get("environment") or {}).get("type") == "cluster":
        lines.append("Every node, job, notebook and container that shares this $HOME "
                     "now uses this one registration.")
    _print(info, getattr(args, "json", False), log, lines)
    return 0 if resolved.paid else 1


def _environment_show(args, log) -> int:
    from plexora.licensing import environment, store

    record = store.read_license()
    env = dict(record.get("environment") or {})
    env["registered"] = bool(environment.binding())
    env["suggested_kind"] = environment.suggested_kind()
    env["scheduler_hint"] = environment.scheduler_hint()
    lines = [
        f"Registered here: {'yes' if env['registered'] else 'no'}",
        f"Name: {env.get('name') or '(none)'}",
        f"Type: {env.get('type') or env['suggested_kind'] + ' (suggested)'}",
        f"Scheduler seen: {env['scheduler_hint']}",
        f"Licence directory: {store.license_dir()}",
    ]
    _print(env, args.json, log, lines)
    return 0


def _refresh(args, log) -> int:
    from plexora.licensing import client, state as license_state

    current = license_state.reload(network=False)
    if not current.certificate or current.source not in ("cache", "token"):
        log("There is no activated licence here to refresh.")
        return 1
    result = client.refresh(current.certificate, timeout=client.INTERACTIVE_TIMEOUT)
    outcome = license_state.apply_refresh(result, current)
    resolved = license_state.current(network=False)
    info = resolved.describe(now=license_state.now())
    _print({"outcome": outcome, **info}, args.json, log,
           [f"Refresh: {outcome}", *status_lines(info)])
    return 0 if outcome in ("ok", "renewed") else 1


def _deactivate(args, log) -> int:
    from plexora.licensing import client, state as license_state, store

    current = license_state.reload(network=False)
    if not current.certificate or current.source != "cache":
        log("There is no activated licence here to deactivate.")
        return 1
    result = client.deactivate(current.certificate)
    store.clear_license()
    license_state.reset()
    lines = ["This environment has been released from its seat, and the local "
             "licence removed. Plexora is on Free here."]
    if result.get("next_allowed_at"):
        lines.append(f"The seat's next environment change is allowed from "
                     f"{_when(result['next_allowed_at'])}.")
    _print(result, args.json, log, lines)
    return 0


def _remove(args, log) -> int:
    from plexora.licensing import environment, state as license_state, store

    removed = store.clear_license()
    if args.forget_environment:
        environment.forget()
    license_state.reset()
    if removed:
        log("Removed the local licence. Plexora is on Free here. The environment is "
            "still registered on its seat; `plexora license deactivate` or the portal "
            "releases it.")
    else:
        log("There was no local licence to remove.")
    return 0


def _fingerprint(args, log) -> int:
    from pathlib import Path

    from plexora.licensing import environment

    report = environment.fingerprint_report(name=args.name,
                                            kind="cluster" if args.cluster else None)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).expanduser().write_text(text + "\n", encoding="utf-8")
        log(f"Wrote {args.out}. Upload it in the licence portal under Offline Licence; "
            f"every field in it is a hash, a family or the name you chose.")
        return 0
    log(text)
    return 0


def _lease(args, log) -> int:
    from plexora.licensing import client, delegation, environment, state as license_state

    current = license_state.reload(network=False)
    if not current.paid or not current.certificate or current.source not in ("cache", "token"):
        log("A job licence can only be minted from a registered environment whose Paid "
            "licence is active here.")
        return 1
    ttl = _parse_ttl(args.ttl)
    if args.server:
        result = client.delegate(current.certificate, ttl_hours=max(1, ttl // 3600))
        log(result["certificate"])
        return 0
    pair = environment.delegation_keypair()
    if pair is None:
        log("This environment has no delegation key. Register it as a cluster "
            "(`plexora license environment register --cluster`), or use --server.")
        return 1
    token = delegation.mint(current.certificate, pair[0], entitlements=args.entitlement,
                            ttl=ttl)
    log(token)
    print("Set it in the job as PLEXORA_LICENSE_JOB_CERT. It needs no network and "
          "registers nothing.", file=sys.stderr)
    return 0


def _trial(args, log) -> int:
    from plexora.licensing import client

    if args.email:
        result = client.start_trial(args.email)
        log(result.get("message") or f"A trial key is on its way to {args.email}. Activate "
                                     f"it with `plexora license activate <key>`.")
        return 0
    url = client.trial_url()
    if not url:
        log("Trials are not available from this build: no licence service is configured.")
        return 1
    log(f"Start a 30-day trial here: {url}")
    log("Or: `plexora license trial --email you@example.org`.")
    return 0
