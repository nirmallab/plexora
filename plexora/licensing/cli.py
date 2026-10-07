"""`plexora license ...` -- see, connect, install and release a licence.

    plexora license                        status (the default)
    plexora license status [--json]
    plexora license activate [CODE | -] [--cluster | --container-host] [--name NAME] [--trial]
    plexora license install FILE|CERTIFICATE
    plexora license refresh
    plexora license deactivate
    plexora license remove [--forget-environment]
    plexora license fingerprint [--name NAME] [--cluster] [--out FILE]
    plexora license lease [--ttl 48h] [--entitlement ENT]
    plexora license trial [--email ADDRESS]

The verbs and what they do are the shared BioCognia client's (`biocognia.cli`,
the same in every BioCognia product); the words are Plexora's. Nothing here is
needed to use Plexora. Free works with no licence at all, and every command
that talks to the platform says so before it does.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys

ACTIONS = ("status", "activate", "install", "refresh", "deactivate", "remove",
           "fingerprint", "lease", "trial")

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
    "environment_mismatch": "the certificate was issued for a different device",
    "unknown_key": "the certificate was signed with a key this Plexora does not trust",
    "bad_signature": "the certificate has been altered",
    "malformed": "the licence file is not a certificate",
    "wrong_product": "the certificate is not for Plexora",
    "unsupported_version": "the certificate needs a newer Plexora",
    "future_dated": "the certificate is dated in the future (check the clock)",
    "unreadable_file": "PLEXORA_LICENSE_FILE could not be read",
    "token_not_exchanged": "BIOCOGNIA_TOKEN has not been exchanged for a certificate",
    "offline_refused": "BIOCOGNIA_OFFLINE forbids exchanging the token",
}

_SOURCE_WORDS = {
    "cache": "connected on this device",
    "token": "BIOCOGNIA_TOKEN",
    "file": "PLEXORA_LICENSE_FILE",
    "job": "PLEXORA_LICENSE_JOB_CERT (a job licence)",
    "install": "an offline licence file installed on this device",
    "none": "none",
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="plexora license",
        description="Show or manage this device's Plexora licence. Free needs no "
                    "licence; Paid unlocks AI features. Sign-in happens at "
                    "account.biocognia.com, never in Plexora.")
    sub = parser.add_subparsers(dest="action")

    status = sub.add_parser("status", help="Show the plan and licence state (default).")
    status.add_argument("--json", action="store_true")

    activate = sub.add_parser("activate", help="Connect this device.")
    activate.add_argument("credential", nargs="?",
                          help="An activation code from account.biocognia.com (BIOC-...), a "
                               "BIOCT1_ token, or - to read one from stdin. Omit it to get a "
                               "code to approve in the browser.")
    activate.add_argument("--key-file", dest="key_file", help="Read the code from a file.")
    activate.add_argument("--name", help="What the portal calls this device.")
    activate.add_argument("--cluster", action="store_true",
                          help="Register this as a whole HPC cluster (run on a login node).")
    activate.add_argument("--container-host", action="store_true", dest="container_host",
                          help="Register a host whose containers run jobs.")
    activate.add_argument("--trial", action="store_true",
                          help="Start a trial if you have no seat.")
    activate.add_argument("--json", action="store_true")

    install = sub.add_parser("install", help="Install an offline licence file or certificate.")
    install.add_argument("source", help="A .bioc file, or a BIOC1 certificate string.")
    install.add_argument("--json", action="store_true")

    refresh = sub.add_parser("refresh", help="Check the licence with the platform now.")
    refresh.add_argument("--json", action="store_true")

    deactivate = sub.add_parser("deactivate",
                                help="Release this device from its seat, then remove the "
                                     "local licence.")
    deactivate.add_argument("--json", action="store_true")

    remove = sub.add_parser("remove", help="Remove the local licence (no network).")
    remove.add_argument("--forget-environment", action="store_true",
                        help="Also discard this device's identity, for EVERY BioCognia "
                             "product. The next activation registers a new device.")

    fingerprint = sub.add_parser("fingerprint",
                                 help="Print what the portal's offline-licence form needs.")
    fingerprint.add_argument("--name")
    fingerprint.add_argument("--cluster", action="store_true")
    fingerprint.add_argument("--out", help="Write the report to this file.")

    lease = sub.add_parser("lease",
                           help="Mint a short job licence from a registered cluster, for "
                                "PLEXORA_LICENSE_JOB_CERT in a job that cannot see $HOME.")
    lease.add_argument("--ttl", default="48h", help="Lifetime, e.g. 12h or 3d (max 7d).")
    lease.add_argument("--entitlement", action="append", dest="entitlements",
                       help="Narrow the job to this entitlement (repeatable).")

    trial = sub.add_parser("trial", help="Start a 30-day Paid trial.")
    trial.add_argument("--email", help="Send the sign-in link here directly instead of "
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
        elif env.get("type") in ("job", "ci"):
            label = "Job licence ended" if ended else "Job licence ends"
        else:
            label = "Ended" if ended else "Valid until"
        lines.append(f"{label}: {_when(until)}")
    if ended and info["state"] == "grace" and info.get("grace_until"):
        lines.append(f"Paid features stop: {_when(info['grace_until'])}")
    renew, renew_by = validity.get("renew"), validity.get("renew_by")
    if renew == "file":
        lines.append(f"Offline licence file works until: {_when(renew_by)}; download a new one "
                     "from account.biocognia.com before then." if renew_by else
                     "This offline licence file has run out; download a new one from "
                     "account.biocognia.com.")
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
                     "`plexora license activate` connects this device, or "
                     "`plexora license trial` starts a trial.")
        return lines
    reason = info.get("reason")
    if info["state"] in ("invalid",) or reason in _REASON_WORDS and info["state"] == "free":
        lines.append(f"Why: {_REASON_WORDS.get(reason, reason)}")
    if info.get("license_id"):
        lines.append(f"Licence: {info['license_id']}"
                     + (f" ({info['use_class']})" if info.get("use_class") else ""))
    if info.get("paid"):
        from plexora.licensing import manifest

        for row in manifest.unlocks(info.get("entitlements"), paid=True):
            lines.append(f"{row['label']}: {'included' if row['granted'] else 'not included'}")
    env = info.get("environment") or {}
    if env.get("type"):
        name = f"{env['name']} " if env.get("name") else ""
        lines.append(f"Device: {name}({env['type']})")
    lines.extend(_validity_lines(info))
    if info.get("source") and info["source"] != "none":
        lines.append(f"From: {_SOURCE_WORDS.get(info['source'], info['source'])}")
    if info["source"] in ("cache", "token") and info["state"] != "invalid":
        lines.append(f"Last checked with the platform: {_when(info.get('last_validated'))}")
    if info.get("clock_rollback"):
        lines.append("This machine's clock is behind a time Plexora has already seen; "
                     "`plexora license refresh` re-checks it online.")
    if info["state"] in ("expired", "revoked"):
        lines.append("Gates, ROIs, figures and everything else you made are untouched; "
                     "only Paid features are off.")
    return lines


def _print(obj, as_json, log, lines=None):
    if as_json:
        log(json.dumps(obj, indent=2, sort_keys=True, default=str))
    else:
        for line in (lines if lines is not None else [str(obj)]):
            log(line)


def run(argv, log=print) -> int:
    parser = build_parser()
    args = parser.parse_args(argv or ["status"])
    action = args.action or "status"

    from plexora.licensing import LICENSING, LicenseError

    try:
        if action == "status":
            LICENSING.reload(network=True)
            info = _describe()
            _print(info, args.json, log, status_lines(info))
            return 0
        if action == "install":
            return _install(args, log)
        if action == "activate":
            return _activate(args, log)
        if action == "refresh":
            return _refresh(args, log)
        if action == "deactivate":
            return _deactivate(args, log)
        if action == "trial":
            return _trial(args, log)
        if action in ("remove", "fingerprint", "lease"):
            return _shared(action, args, log)
    except (LicenseError, OSError, ValueError) as exc:
        log(str(exc))
        return 1
    except KeyboardInterrupt:
        log("Stopped.")
        return 130
    parser.print_help()
    return 2


def _describe() -> dict:
    from plexora import licensing

    return licensing.describe(network=False)


def _install(args, log) -> int:
    from pathlib import Path

    from biocognia import certificate, store

    from plexora import licensing
    from plexora.licensing import LICENSING

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
    installed = LICENSING.install_certificate(cert, source="install")
    info = licensing.describe(state=installed)
    _print(info, args.json, log, ["Installed.", *status_lines(info)])
    return 0 if installed.licensed else 1


def _activate(args, log) -> int:
    from plexora.licensing import LICENSING

    credential = args.credential
    if args.key_file:
        with open(args.key_file, encoding="utf-8") as handle:
            credential = handle.read().strip()
    elif credential == "-":
        credential = sys.stdin.readline().strip()
    kind = "cluster" if args.cluster else "container-host" if args.container_host else None

    def show(code, url):
        log(f"Open {url} and enter {code}")
        log("Waiting for approval in the browser (Ctrl-C to stop)...")

    resolved = LICENSING.activate(credential or None, kind=kind, name=args.name,
                                  trial=args.trial, on_code=show)
    info = _describe()
    lines = ["Activated.", *status_lines(info)]
    if (info.get("environment") or {}).get("type") == "cluster":
        lines.append("Every node, job, notebook and container that shares this $HOME "
                     "now uses this one registration.")
    _print(info, getattr(args, "json", False), log, lines)
    return 0 if resolved.licensed else 1


def _refresh(args, log) -> int:
    from plexora.licensing import LICENSING

    current = LICENSING.reload(network=False)
    if not current.certificate or current.source not in ("cache", "token"):
        log("There is no activated licence here to refresh.")
        return 1
    # Asked for by a person: the background refresh's schedule and switch do
    # not apply, BIOCOGNIA_OFFLINE does (the client refuses with a sentence).
    result = LICENSING.client.refresh(current.certificate, timeout=15)
    outcome = LICENSING.apply_refresh(result, current)
    info = _describe()
    _print({"outcome": outcome, **info}, args.json, log,
           [f"Refresh: {outcome}", *status_lines(info)])
    return 0 if outcome in ("ok", "renewed") else 1


def _deactivate(args, log) -> int:
    from plexora.licensing import LICENSING

    current = LICENSING.reload(network=False)
    if not current.certificate or current.source != "cache":
        log("There is no activated licence here to deactivate.")
        return 1
    result = LICENSING.deactivate()
    lines = ["This device has been released from its seat, and the local licence "
             "removed. Plexora is on Free here."]
    if result.get("next_allowed_at"):
        lines.append(f"The seat's next device change is allowed from "
                     f"{_when(result['next_allowed_at'])}.")
    _print(result, args.json, log, lines)
    return 0


def _trial(args, log) -> int:
    from biocognia import store

    from plexora.licensing import LICENSING, PRODUCT

    if args.email:
        result = LICENSING.client.start_trial(args.email)
        log(result.get("message") or f"A sign-in link is on its way to {args.email}. Approve "
                                     f"the trial there, then run `plexora license activate`.")
        return 0
    log(f"Start a {PRODUCT.trial_days}-day trial here: "
        f"{store.portal_url(f'start?product={PRODUCT.id}')}")
    log("Then connect this device with `plexora license activate`, or "
        "`plexora license trial --email you@example.org`.")
    return 0


def _shared(action: str, args, log) -> int:
    """`remove`, `fingerprint` and `lease`: the shared verbs, worded by the library."""
    from biocognia import cli as shared

    from plexora.licensing import LICENSING

    namespace = argparse.Namespace(license_command=action, **vars(args))
    if action == "fingerprint" and not args.out:
        namespace.out = None
    code = shared.run(namespace, LICENSING, out=log, err=log)
    if action == "lease" and code == 0:
        print("Set it in the job as PLEXORA_LICENSE_JOB_CERT. It needs no network and "
              "registers nothing.", file=sys.stderr)
    return code
