"""The public halves of the keys that sign Plexora licence certificates.

Public keys only. The private halves live as Cloudflare Worker secrets and in
one offline copy; they are never in this repository, never in the wheel, and
never on a machine that runs Plexora. Holding everything in this file lets
somebody verify a certificate and nothing else.

Two slots: `px1` signs today, `px2` is the standby a rotation moves to. A build
must trust a kid before the server signs with it, so a new kid ships here one
release ahead of its first use. Retiring a kid is removing it here -- every
certificate it signed then reads as `unknown_key` and falls back to Free.

Regenerate with `licensing/tools/generate_keys.py`.
"""

from __future__ import annotations

import base64

#: kid -> base64url (unpadded) raw 32-byte Ed25519 public key.
PUBLIC_KEYS: dict[str, str] = {
    # generated 2026-09-27T21:58:43+00:00
    "px1": "jQttSzXwG5urWuMpQxXKqFMwKuKEBv3XiqfuvvtuK-4",
    # generated 2026-09-27T21:58:43+00:00
    "px2": "RaXE_ZCds7Wlds55oNLIesOozWA-T4N_0SpmCl_QC30",
}

PRIMARY_KID = "px1"


def raw(value: str) -> bytes:
    """A key table entry as the 32 raw bytes `cryptography` wants."""
    padded = value + "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode(padded)
    if len(decoded) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    return decoded
