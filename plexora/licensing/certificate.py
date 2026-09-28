"""Parse and verify Plexora licence certificates.

Wire format, four dot-separated fields::

    PLEXORA1.<kid>.<base64url(payload JSON)>.<base64url(Ed25519 signature)>

The signature covers the raw payload bytes, exactly what field 3 decodes to.
Deliberately not a JWT: no algorithm field to confuse, no library to keep
patched, and one format small enough that the Worker that signs it
(`licensing/src/certs.ts`) and this verifier are tested against the same bytes
(`licensing/vectors/plexora-license-vectors.json`).

The payload is canonical JSON -- keys sorted, no whitespace, UTF-8, integers
only -- so both signers produce identical bytes for identical claims. `kid`
appears in the header and inside the payload and the two must agree: the header
chooses the key, the payload copy is what the signature attests to. `aud` must
be `plexora`, so a certificate minted for any other product that happened to
share a key would still be refused.

Expiry is not checked here. An expired certificate is still authentic; whether
it is inside its grace window is a question for `state.py`.

`cryptography` is imported inside `verify`, not at module scope: importing
Plexora, and every Free path after it, never loads it.
"""

from __future__ import annotations

import base64
import json
import time

from plexora.licensing.errors import (BadSignature, FutureDated, MalformedCertificate,
                                      UnknownKey, UnsupportedVersion, WrongProduct)

PREFIX = "PLEXORA1"
AUDIENCE = "plexora"
VERSION = 1

#: A certificate issued by a server whose clock is slightly ahead of ours is
#: normal. One dated an hour ahead is a clock problem worth naming.
MAX_CLOCK_SKEW_SECONDS = 300

#: Far larger than any real certificate (under 2 KB); a bound on what a
#: hand-edited file can make this parse.
MAX_LENGTH = 16 * 1024


def b64encode(raw: bytes) -> str:
    """Base64url without padding, as both signers emit it."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64decode(text: str, what: str = "field") -> bytes:
    if not isinstance(text, str) or not text or any(c in text for c in "+/="):
        raise MalformedCertificate(f"the certificate has an unreadable {what}")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except Exception as exc:  # noqa: BLE001 - re-raised as our own type
        raise MalformedCertificate(f"the certificate has an unreadable {what}") from exc


def canonical_json(value) -> bytes:
    """The one serialisation both signers use."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def looks_like(text) -> bool:
    """Whether `text` is shaped like a certificate, without verifying it."""
    return isinstance(text, str) and text.strip().startswith(PREFIX + ".")


def parse(certificate: str) -> tuple[str, dict, bytes, bytes]:
    """Split a certificate into `(kid, payload, signature, signed_bytes)`.

    No cryptography and no claims checks; `verify` does both.
    """
    if not isinstance(certificate, str):
        raise MalformedCertificate("a certificate is a string")
    text = certificate.strip()
    if len(text) > MAX_LENGTH:
        raise MalformedCertificate("the certificate is too long to be one")
    parts = text.split(".")
    if len(parts) != 4:
        raise MalformedCertificate(
            f"a certificate has four dot-separated fields; this has {len(parts)}")
    prefix, kid, payload_text, signature_text = parts
    if prefix != PREFIX:
        raise MalformedCertificate(
            f"unknown certificate format {prefix!r}; this Plexora reads {PREFIX!r}. "
            f"A newer Plexora may be able to read it.")
    if not kid:
        raise MalformedCertificate("the certificate names no signing key")
    signed = b64decode(payload_text, "payload")
    signature = b64decode(signature_text, "signature")
    try:
        payload = json.loads(signed.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - re-raised as our own type
        raise MalformedCertificate("the certificate payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise MalformedCertificate("the certificate payload is not an object")
    return kid, payload, signature, signed


def verify_signature(public_key: bytes, signature: bytes, signed: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, signed)
    except (InvalidSignature, ValueError):
        return False
    return True


def verify(certificate: str, *, now: float | None = None, keys: dict | None = None) -> dict:
    """The payload of `certificate`, once its signature and claims check out.

    `keys` maps kid -> base64url public key and defaults to the shipped table;
    tests pass their own. Raises a `LicenseError` subclass for anything that
    makes the certificate untrustworthy.
    """
    from plexora.licensing import keys as key_table

    kid, payload, signature, signed = parse(certificate)
    table = key_table.PUBLIC_KEYS if keys is None else keys
    encoded = table.get(kid)
    if encoded is None:
        raise UnknownKey(
            f"this certificate was signed with key {kid!r}, which this version of "
            f"Plexora does not trust. Upgrade Plexora, or ask for a certificate "
            f"signed with a current key.")
    try:
        public = key_table.raw(encoded)
    except (ValueError, TypeError) as exc:
        raise UnknownKey(f"the trusted key {kid!r} is not a valid public key") from exc
    if not verify_signature(public, signature, signed):
        raise BadSignature(
            "the certificate signature does not verify; it has been altered or "
            "truncated. Download it again from the licence portal.")
    check_claims(payload, kid=kid, now=now)
    return payload


def check_claims(payload: dict, *, kid: str | None = None, now: float | None = None) -> None:
    """The checks every certificate kind shares, after its signature verified."""
    if kid is not None and payload.get("kid") != kid:
        raise BadSignature("the certificate names two different signing keys")
    if payload.get("aud") != AUDIENCE:
        raise WrongProduct("this certificate is not for Plexora")
    if payload.get("v") != VERSION:
        raise UnsupportedVersion(
            f"certificate version {payload.get('v')!r} is not supported by this "
            f"Plexora; upgrade it.")
    moment = time.time() if now is None else now
    issued = payload.get("issued_at")
    if isinstance(issued, (int, float)) and issued > moment + MAX_CLOCK_SKEW_SECONDS:
        raise FutureDated("the certificate is dated in the future; check this machine's clock")
