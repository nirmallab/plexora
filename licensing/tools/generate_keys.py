#!/usr/bin/env python3
"""Generate the Ed25519 signing keypairs for Plexora licence certificates.

Run once at setup, and again only for a planned rotation. The private keys are
written to the file named by --out, which must be OUTSIDE this repository and
outside any synced folder: a signing key that Dropbox, iCloud, OneDrive or
Google Drive has copied to its servers and to every linked device is a key that
has left your control, and every certificate it signs can be forged by anybody
who has it.

    python licensing/tools/generate_keys.py --out ~/.plexora-license-keys/signing-keys.json
    python licensing/tools/generate_keys.py --out ... --kid px3    # one rotation key

After running:
  1. Paste the printed PUBLIC_KEYS block into plexora/licensing/keys.py.
  2. `wrangler secret put SIGNING_KEY_PX1` (and PX2), typing each private
     value in from the file -- never from a file inside the repository.
  3. Copy the file into a password manager or onto encrypted offline media,
     then delete it from disk.

Only the public halves are ever printed. This file is a repository tool; it is
never packaged into the wheel.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Path fragments that mean "a sync client uploads this directory". Matched
#: case-insensitively against the resolved path.
SYNCED_MARKERS = ("dropbox", "mobile documents", "icloud", "onedrive",
                  "google drive", "googledrive", "box sync", "/box/")


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def refuse_unsafe(path: Path, verb: str = "write") -> None:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(REPO)
    except ValueError:
        pass
    else:
        raise SystemExit(f"Refusing to {verb} signing keys inside the repository ({resolved}).")
    lowered = str(resolved).lower()
    for marker in SYNCED_MARKERS:
        if marker in lowered:
            raise SystemExit(
                f"Refusing to {verb} signing keys at {resolved}: it looks like a synced "
                f"folder ({marker.strip('/')!r}). Choose a local, unsynced directory.")


def make_key(kid: str) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (Encoding, NoEncryption,
                                                              PrivateFormat, PublicFormat)

    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {"kid": kid, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "private_b64url": b64url(seed), "public_b64url": b64url(public)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path,
                        help="where to write the private keys (outside the repo and any synced folder)")
    parser.add_argument("--kid", action="append",
                        help="key id to generate; repeatable. Defaults to px1 and px2.")
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = parser.parse_args(argv)

    out = args.out.expanduser()
    refuse_unsafe(out)
    if out.exists() and not args.force:
        raise SystemExit(f"{out} exists. Refusing to overwrite signing keys without --force; "
                         f"losing them invalidates every certificate they signed.")
    kids = args.kid or ["px1", "px2"]
    keys = [make_key(kid) for kid in kids]

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(out.parent, 0o700)
    except OSError:
        pass
    tmp = out.with_name(out.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"product": "plexora", "keys": keys}, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, out)

    print(f"Wrote {len(keys)} keypair(s) to {out} (mode 0600).", file=sys.stderr)
    print("\nPaste into plexora/licensing/keys.py:\n")
    print("PUBLIC_KEYS: dict[str, str] = {")
    for key in keys:
        print(f'    # generated {key["created"]}')
        print(f'    "{key["kid"]}": "{key["public_b64url"]}",')
    print("}\n")
    print("PUBLIC_KEYS_JSON for licensing/wrangler.toml [vars]:\n")
    print(json.dumps({k["kid"]: k["public_b64url"] for k in keys}, separators=(",", ":")))
    print("\nLoad the private halves into the Worker (value = private_b64url, typed in):\n")
    for key in keys:
        print(f"    npx wrangler secret put SIGNING_KEY_{key['kid'].upper()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
