"""Job certificates: a registered cluster lending its licence to one job.

A cluster registers once, from a login node, and its certificate lives in
`$HOME` where every node can read it -- which covers the ordinary case with no
extra machinery at all. This module is for the case that does not share
`$HOME`: a container with only a scratch mount, a cloud batch VM, a node image
with a read-only home. There, the primary mints a short-lived JOB CERTIFICATE
and hands it to the job in `PLEXORA_LICENSE_JOB_CERT`.

    PLEXORAD1.<base64url(payload JSON)>.<base64url(Ed25519 signature)>

The payload carries the full parent certificate. The parent was signed by the
licence server and names a DELEGATION public key the server embedded when the
cluster registered; the job certificate is signed by the matching private key,
which never leaves the cluster's `environment.json`. So a job verifies, with
nothing but the public keys shipped in Plexora:

    shipped key -> parent certificate -> its delegation key -> job certificate

and never talks to the server, never registers, never counts against a seat.
What a job gets is the INTERSECTION of what it asked for and what the parent
grants, until the EARLIER of the two expiries, with no grace period, and for at
most seven days -- bounded, because nothing can recall it before then.
"""

from __future__ import annotations

import json
import secrets
import time

from plexora.licensing import certificate
from plexora.licensing.entitlements import any_satisfies, normalize
from plexora.licensing.errors import BadSignature, LicenseError, MalformedCertificate

PREFIX = "PLEXORAD1"
MAX_LIFETIME_SECONDS = 7 * 86400
DEFAULT_TTL_SECONDS = 48 * 3600
DELEGATING_TYPES = ("cluster", "container-host")


def looks_like(text) -> bool:
    return isinstance(text, str) and text.strip().startswith(PREFIX + ".")


def mint(parent: str, seed: bytes, *, entitlements=None, ttl: float = DEFAULT_TTL_SECONDS,
         now: float | None = None, keys: dict | None = None) -> str:
    """A job certificate under `parent`, signed with the delegation `seed`.

    Refuses a parent that does not verify, that carries no delegation key, or
    whose key is not the one `seed` produces: a job certificate that every
    job would reject is worse than an error at the moment it was asked for.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    moment = int(time.time() if now is None else now)
    parent_payload = certificate.verify(parent, now=moment, keys=keys)
    if parent_payload.get("environment_type") not in DELEGATING_TYPES:
        raise LicenseError("only a registered cluster or container host can lend its "
                           "licence to jobs", code="not_delegating")
    private = Ed25519PrivateKey.from_private_bytes(seed)
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    if certificate.b64encode(public) != parent_payload.get("delegation_pubkey"):
        raise LicenseError("this environment's delegation key does not match its "
                           "certificate; register the environment again",
                           code="delegation_mismatch")
    granted = normalize(parent_payload.get("entitlements"))
    wanted = granted if entitlements is None else normalize(list(entitlements))
    lifetime = int(max(60, min(float(ttl), MAX_LIFETIME_SECONDS)))
    expires = min(moment + lifetime, int(parent_payload.get("expires_at") or 0))
    if expires <= moment:
        raise LicenseError("this environment's certificate has expired; it cannot "
                           "lend a licence to a job", code="license_expired")
    payload = {
        "v": certificate.VERSION,
        "aud": certificate.AUDIENCE,
        "kind": "job",
        "job_id": "job_" + secrets.token_hex(8),
        "parent": parent.strip(),
        "entitlements": [e for e in wanted if any_satisfies(granted, e)],
        "issued_at": moment,
        "expires_at": expires,
    }
    body = certificate.canonical_json(payload)
    signature = private.sign(body)
    return f"{PREFIX}.{certificate.b64encode(body)}.{certificate.b64encode(signature)}"


def verify(text: str, *, now: float | None = None, keys: dict | None = None) -> dict:
    """The EFFECTIVE payload of a job certificate: the parent's claims with the
    job's narrower grants, earlier expiry and no grace."""
    from plexora.licensing import keys as key_table

    if not isinstance(text, str):
        raise MalformedCertificate("a job certificate is a string")
    parts = text.strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise MalformedCertificate(f"a job certificate starts {PREFIX!r} and has three fields")
    body = certificate.b64decode(parts[1], "payload")
    signature = certificate.b64decode(parts[2], "signature")
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise MalformedCertificate("the job certificate payload is not JSON") from exc
    if not isinstance(payload, dict) or payload.get("kind") != "job":
        raise MalformedCertificate("not a job certificate")

    parent_payload = certificate.verify(payload.get("parent") or "", now=now, keys=keys)
    if parent_payload.get("environment_type") not in DELEGATING_TYPES:
        raise BadSignature("the job certificate's parent cannot delegate")
    delegation = parent_payload.get("delegation_pubkey")
    if not isinstance(delegation, str) or not delegation:
        raise BadSignature("the job certificate's parent names no delegation key")
    try:
        public = key_table.raw(delegation)
    except (ValueError, TypeError) as exc:
        raise BadSignature("the parent's delegation key is not a valid key") from exc
    if not certificate.verify_signature(public, signature, body):
        raise BadSignature("the job certificate signature does not verify")
    certificate.check_claims(payload, now=now)

    issued = payload.get("issued_at")
    expires = payload.get("expires_at")
    if not isinstance(issued, int) or not isinstance(expires, int):
        raise MalformedCertificate("the job certificate has no validity window")
    if expires - issued > MAX_LIFETIME_SECONDS + certificate.MAX_CLOCK_SKEW_SECONDS:
        raise BadSignature("the job certificate claims a longer life than a job may have")
    granted = normalize(parent_payload.get("entitlements"))
    effective = [e for e in normalize(payload.get("entitlements")) if any_satisfies(granted, e)]
    return {
        **parent_payload,
        "environment_type": "job",
        "cert_id": payload.get("job_id"),
        "parent_cert_id": parent_payload.get("cert_id"),
        "entitlements": effective,
        "issued_at": issued,
        "expires_at": min(expires, int(parent_payload.get("expires_at") or expires)),
        "grace_days": 0,
        "offline_until": None,
        "env_binding": None,
    }
