#!/usr/bin/env python3
"""Sign one Plexora certificate by hand, outside the licence service.

For two situations only:

* DEVELOPMENT, with a throwaway key: generate one with
  `generate_keys.py --out /tmp/dev-keys.json --kid pxdev`, point a local
  Plexora at it by adding its public half to a scratch copy of keys.py, and
  mint certificates for any state you want to look at.
* EMERGENCY offline issuance with a production key from its offline copy,
  when the service is down and somebody must have a licence file today.

Everything the service does -- the environment cap, the seat, the audit log,
the offline grant record -- is skipped here. A certificate made this way is
known to nothing; record it by hand, and prefer the admin portal whenever it
is up. The keys file must be outside the repository and any synced folder,
exactly as generate_keys.py requires.

    python licensing/tools/issue_license.py --keys ~/.plexora-license-keys/signing-keys.json \\
        --kid px1 --binding <from fp.json> --days 90 --offline --out lab.plexora
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_keys import b64url, refuse_unsafe  # noqa: E402

KINDS = ("desktop", "cluster", "container-host", "job")
USE_CLASSES = ("academic", "commercial", "nonprofit", "government")


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load_seed(path: Path, kid: str) -> bytes:
    document = json.loads(path.expanduser().read_text(encoding="utf-8"))
    for key in document.get("keys", []):
        if key.get("kid") == kid:
            text = key["private_b64url"]
            return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    raise SystemExit(f"no key {kid!r} in {path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keys", required=True, type=Path)
    parser.add_argument("--kid", required=True)
    parser.add_argument("--binding", help="env binding (sha256 hex) from a fingerprint report; "
                                          "omit only for a job certificate")
    parser.add_argument("--report", type=Path, help="read binding/kind/delegation from fp.json")
    parser.add_argument("--kind", choices=KINDS, default="desktop")
    parser.add_argument("--days", type=float, default=30)
    parser.add_argument("--offline", action="store_true", help="mark as an offline certificate")
    parser.add_argument("--trial", action="store_true")
    parser.add_argument("--grace-days", type=int, default=14)
    parser.add_argument("--use-class", choices=USE_CLASSES, default="academic")
    parser.add_argument("--entitlement", action="append", help="repeatable; default: ai")
    parser.add_argument("--license-id", default=None)
    parser.add_argument("--out", type=Path, help="write a .plexora file instead of printing")
    args = parser.parse_args(argv)
    refuse_unsafe(args.keys, "use")

    binding, kind, delegation = args.binding, args.kind, None
    if args.report:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        binding = report.get("binding") or binding
        kind = report.get("kind") or kind
        delegation = report.get("delegation_pubkey")
    if kind != "job" and not (binding and len(binding) == 64):
        raise SystemExit("a non-job certificate needs --binding (or --report)")

    now = int(time.time())
    expires = now + int(args.days * 86400)
    tag = hashlib.sha256(secrets.token_bytes(16)).hexdigest()[:16]
    payload = {
        "v": 1, "aud": "plexora", "kid": args.kid, "cert_id": f"crt_manual_{tag}",
        "license_id": args.license_id or f"lic_manual_{tag}", "account_id": "acc_manual",
        "seat_id": "sa_manual", "plan": "paid", "trial": bool(args.trial),
        "use_class": args.use_class, "entitlements": sorted(set(args.entitlement or ["ai"])),
        "environment_id": None if kind == "job" else f"env_manual_{tag}",
        "environment_type": kind, "env_binding": None if kind == "job" else binding,
        "delegation_pubkey": delegation if kind in ("cluster", "container-host") else None,
        "issued_at": now, "expires_at": expires, "license_expires_at": expires,
        "grace_days": 0 if args.trial or kind == "job" else args.grace_days,
        "offline_until": expires if args.offline else None,
    }
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(load_seed(args.keys, args.kid))
    body = canonical(payload)
    certificate = f"PLEXORA1.{args.kid}.{b64url(body)}.{b64url(private.sign(body))}"
    if args.out:
        until = time.strftime("%Y-%m-%d", time.gmtime(expires))
        args.out.write_text("\n".join([
            "# Plexora licence (issued by hand -- not recorded by the licence service)",
            f"# Valid until {until}. Install: plexora license install <this file>",
            certificate, ""]), encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(certificate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
