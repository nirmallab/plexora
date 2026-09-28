"""Certificate verification: what is accepted, and every way to be refused."""

import json
import time

import pytest

from plexora.licensing import certificate
from plexora.licensing.errors import (BadSignature, FutureDated, MalformedCertificate,
                                      UnknownKey, UnsupportedVersion, WrongProduct)


def test_round_trip(license_issuer):
    cert = license_issuer.issue()
    payload = certificate.verify(cert)
    assert payload["plan"] == "paid"
    assert payload["entitlements"] == ["ai"]
    assert certificate.looks_like(cert)


def test_payload_is_canonical_json(license_issuer):
    payload = license_issuer.payload()
    cert = license_issuer.sign(payload)
    _, parsed, _, signed = certificate.parse(cert)
    assert signed == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert parsed == payload


def test_expired_certificate_still_verifies(license_issuer):
    """Expiry is the state layer's question, not the verifier's."""
    cert = license_issuer.issue(issued_at=1, expires_at=2)
    assert certificate.verify(cert)["expires_at"] == 2


def _swap_payload(cert, payload):
    prefix, kid, _, sig = cert.split(".")
    body = certificate.b64encode(certificate.canonical_json(payload))
    return ".".join([prefix, kid, body, sig])


def test_modified_payload_is_refused(license_issuer):
    cert = license_issuer.issue(entitlements=["ai:evidence"])
    forged = _swap_payload(cert, license_issuer.payload(entitlements=["ai"]))
    with pytest.raises(BadSignature):
        certificate.verify(forged)


def test_extended_expiry_is_refused(license_issuer):
    cert = license_issuer.issue()
    payload = license_issuer.payload(expires_at=int(time.time()) + 10 * 365 * 86400)
    with pytest.raises(BadSignature):
        certificate.verify(_swap_payload(cert, payload))


def test_truncated_signature_is_refused(license_issuer):
    cert = license_issuer.issue()
    with pytest.raises((BadSignature, MalformedCertificate)):
        certificate.verify(cert[:-6])


def test_unknown_kid_is_refused(license_issuer):
    cert = license_issuer.sign(license_issuer.payload(kid="zz9"), kid="zz9")
    with pytest.raises(UnknownKey):
        certificate.verify(cert)


def test_header_and_payload_kid_must_agree(license_issuer, monkeypatch):
    from plexora.licensing import keys

    monkeypatch.setattr(keys, "PUBLIC_KEYS", {"pxt": license_issuer.public_b64,
                                              "other": license_issuer.public_b64})
    cert = license_issuer.sign(license_issuer.payload(kid="other"), kid="pxt")
    with pytest.raises(BadSignature):
        certificate.verify(cert)


def test_another_products_certificate_is_refused(license_issuer):
    with pytest.raises(WrongProduct):
        certificate.verify(license_issuer.issue(aud="scimappro"))
    with pytest.raises(WrongProduct):
        certificate.verify(license_issuer.sign({k: v for k, v in
                                                license_issuer.payload().items()
                                                if k != "aud"}))


def test_future_version_is_refused(license_issuer):
    with pytest.raises(UnsupportedVersion):
        certificate.verify(license_issuer.issue(v=2))


def test_future_dated_is_refused_beyond_skew(license_issuer):
    now = int(time.time())
    certificate.verify(license_issuer.issue(issued_at=now + 200), now=now)
    with pytest.raises(FutureDated):
        certificate.verify(license_issuer.issue(issued_at=now + 3600), now=now)


def test_shipped_keys_do_not_trust_a_test_certificate(license_issuer, monkeypatch):
    from plexora.licensing import keys

    cert = license_issuer.issue()
    monkeypatch.setattr(keys, "PUBLIC_KEYS", {"px1": keys.PUBLIC_KEYS.get("pxt")
                                              or license_issuer.public_b64})
    with pytest.raises(UnknownKey):
        certificate.verify(cert)


@pytest.mark.parametrize("text", [
    "", "PLEXORA1", "PLEXORA1.pxt.abc", "SCIMAPPRO1.k1.e30.AA", "PLEXORA2.pxt.e30.AA",
    "PLEXORA1..e30.AA", "PLEXORA1.pxt.!!!.AA", "PLEXORA1.pxt.bm90IGpzb24.AA",
    "PLEXORA1.pxt.WzFd.AA",  # a JSON list, not an object
    "PLEXORA1.pxt.e30.AA+/",
])
def test_malformed(text, license_issuer):
    with pytest.raises((MalformedCertificate, UnknownKey, BadSignature)):
        certificate.verify(text)


def test_non_string_and_huge_input():
    with pytest.raises(MalformedCertificate):
        certificate.parse(None)
    with pytest.raises(MalformedCertificate):
        certificate.parse("PLEXORA1." + "a" * 20000)


def test_shipped_key_table_is_well_formed():
    from plexora.licensing import keys

    assert keys.PRIMARY_KID in keys.PUBLIC_KEYS
    for kid, value in keys.PUBLIC_KEYS.items():
        assert len(keys.raw(value)) == 32, kid


def test_no_private_key_material_ships():
    """The package holds public keys only: nothing in it can sign."""
    from pathlib import Path

    import plexora.licensing as package

    root = Path(package.__file__).parent
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "private_b64" not in text, path
        assert "BEGIN PRIVATE KEY" not in text, path
        assert "SIGNING_KEY_" not in text, path
