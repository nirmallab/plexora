#!/usr/bin/env python3
"""Write vectors/plexora-license-vectors.json: the byte-level contract between
the Worker's signer (src/certs.ts) and Plexora's verifier
(plexora/licensing/certificate.py, delegation.py).

Ed25519 is deterministic, so a fixed seed and a fixed payload produce one
exact certificate string. The vitest suite re-signs every payload and must
produce the same bytes; the pytest suite verifies every certificate and must
accept exactly the ones marked valid.

The seeds are FIXED TEST VALUES derived from a public label. The `vx1` kid is
trusted by nothing -- not the shipped key table, not a deployed Worker -- so
publishing these vectors grants nothing.

    python licensing/tools/make_test_vectors.py
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

OUT = Path(__file__).resolve().parents[1] / "vectors" / "plexora-license-vectors.json"
KID = "vx1"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def seed(label: str) -> bytes:
    return hashlib.sha256(f"plexora licensing test vectors: {label}".encode()).digest()


def public_of(private: Ed25519PrivateKey) -> str:
    return b64url(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def sign_cert(private: Ed25519PrivateKey, payload: dict) -> str:
    body = canonical(payload)
    return f"PLEXORA1.{payload['kid']}.{b64url(body)}.{b64url(private.sign(body))}"


def sign_job(private: Ed25519PrivateKey, payload: dict) -> str:
    body = canonical(payload)
    return f"PLEXORAD1.{b64url(body)}.{b64url(private.sign(body))}"


def base(**overrides) -> dict:
    payload = {
        "v": 1, "aud": "plexora", "kid": KID, "cert_id": "crt_0000000000000001",
        "license_id": "lic_0000000000000001", "account_id": "acc_0000000000000001",
        "seat_id": "sa_0000000000000001", "plan": "paid", "trial": False, "use_class": "academic",
        "entitlements": ["ai"], "environment_id": "env_0000000000000001",
        "environment_type": "desktop",
        "env_binding": hashlib.sha256(b"vector environment secret").hexdigest(),
        "delegation_pubkey": None, "issued_at": 1767225600, "expires_at": 1774998000,
        "grace_days": 14, "offline_until": None,
    }
    payload.update(overrides)
    return payload


def main() -> int:
    signer_seed = seed("signing key vx1")
    signer = Ed25519PrivateKey.from_private_bytes(signer_seed)
    delegation_seed = seed("delegation key")
    delegate = Ed25519PrivateKey.from_private_bytes(delegation_seed)

    cases = []

    def add(name, payload, *, valid=True, reason=None, tamper=None):
        cert = sign_cert(signer, payload)
        if tamper == "payload":
            prefix, kid, _, sig = cert.split(".")
            other = dict(payload, entitlements=["ai", "plugin:extra"])
            cert = ".".join([prefix, kid, b64url(canonical(other)), sig])
        elif tamper == "kid_header":
            prefix, _, body, sig = cert.split(".")
            cert = ".".join([prefix, "vx2", body, sig])
        cases.append({"name": name, "payload": payload,
                      "canonical": canonical(payload).decode("utf-8"),
                      "certificate": cert, "valid": valid, "reason": reason})

    add("desktop", base())
    add("trial", base(trial=True, grace_days=0, cert_id="crt_0000000000000002"))
    add("offline", base(offline_until=1782000000, expires_at=1782000000, cert_id="crt_0000000000000003"))
    add("narrow_grants", base(entitlements=["ai:evidence", "ai:gating:session"], cert_id="crt_0000000000000004"))
    # Non-ASCII survives as raw UTF-8 on both sides (no \\u escapes).
    add("unicode_and_nulls", base(use_class="commercial", cert_id="crt_ünïcødé_0005", environment_id=None))
    add("tampered_payload", base(cert_id="crt_0000000000000006"), valid=False, reason="bad_signature",
        tamper="payload")
    add("wrong_audience", base(aud="scimappro", cert_id="crt_0000000000000007"), valid=False,
        reason="wrong_product")
    add("header_kid_rewritten", base(cert_id="crt_0000000000000008"), valid=False, reason="unknown_key",
        tamper="kid_header")

    cluster = base(environment_type="cluster", delegation_pubkey=public_of(delegate),
                   cert_id="crt_0000000000000009")
    cluster_cert = sign_cert(signer, cluster)
    job_payload = {"v": 1, "aud": "plexora", "kind": "job", "job_id": "job_0000000000000001",
                   "parent": cluster_cert, "entitlements": ["ai:gating"],
                   "issued_at": 1767225600, "expires_at": 1767225600 + 48 * 3600}
    jobs = [{"name": "job_under_cluster", "parent": cluster_cert, "payload": job_payload,
             "canonical": canonical(job_payload).decode("utf-8"),
             "certificate": sign_job(delegate, job_payload), "valid": True,
             "effective_entitlements": ["ai:gating"],
             "effective_expires_at": job_payload["expires_at"]}]

    document = {
        "schema": 1,
        "note": "TEST VECTORS ONLY. The vx1 key is trusted by nothing; see tools/make_test_vectors.py.",
        "kid": KID,
        "public_key": public_of(signer),
        "seed": b64url(signer_seed),
        "delegation_seed": b64url(delegation_seed),
        "delegation_public_key": public_of(delegate),
        "paid_entitlements": ["ai"],
        "certificates": cases,
        "jobs": jobs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(cases)} certificates, {len(jobs)} job certificates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
