"""Job certificates: a registered cluster lending its licence to one job,
verified with nothing but the shipped public keys."""

import json
import time

import pytest

from plexora import licensing
from plexora.licensing import certificate, delegation, environment, store
from plexora.licensing.errors import BadSignature, LicenseError, MalformedCertificate


@pytest.fixture
def cluster(license_issuer):
    """A cluster registration: its certificate names its delegation key."""
    seed, public = environment.delegation_keypair(create=True)
    parent = license_issuer.issue(environment_type="cluster", delegation_pubkey=public,
                                  entitlements=["ai"])
    return parent, seed


def test_mint_and_verify(cluster):
    parent, seed = cluster
    job = delegation.mint(parent, seed, ttl=3600)
    payload = delegation.verify(job)
    assert payload["environment_type"] == "job"
    assert payload["entitlements"] == ["ai"]
    assert payload["grace_days"] == 0 and payload["env_binding"] is None
    assert payload["expires_at"] - payload["issued_at"] <= 3600


def test_job_state_through_the_environment_variable(cluster, monkeypatch):
    parent, seed = cluster
    environment.forget()      # the job cannot see the cluster's $HOME
    monkeypatch.setenv(store.ENV_JOB_CERT, delegation.mint(parent, seed))
    st = licensing.current()
    assert st.source == "job" and st.environment_type == "job"
    assert licensing.allows("ai:gating")


def test_grants_are_intersected(license_issuer):
    seed, public = environment.delegation_keypair(create=True)
    parent = license_issuer.issue(environment_type="cluster", delegation_pubkey=public,
                                  entitlements=["ai:evidence"])
    payload = delegation.verify(delegation.mint(parent, seed,
                                                entitlements=["ai", "ai:evidence"]))
    assert payload["entitlements"] == ["ai:evidence"], "a job never gains a grant"


def test_expiry_is_the_earlier_of_the_two(license_issuer):
    seed, public = environment.delegation_keypair(create=True)
    soon = int(time.time()) + 600
    parent = license_issuer.issue(environment_type="cluster", delegation_pubkey=public,
                                  expires_at=soon)
    assert delegation.verify(delegation.mint(parent, seed, ttl=86400))["expires_at"] == soon


def test_ttl_is_capped_at_seven_days(cluster):
    parent, seed = cluster
    payload = delegation.verify(delegation.mint(parent, seed, ttl=90 * 86400))
    assert payload["expires_at"] - payload["issued_at"] <= delegation.MAX_LIFETIME_SECONDS


def test_desktop_cannot_delegate(license_issuer):
    seed, public = environment.delegation_keypair(create=True)
    parent = license_issuer.issue(environment_type="desktop", delegation_pubkey=public)
    with pytest.raises(LicenseError):
        delegation.mint(parent, seed)


def test_wrong_delegation_key_is_refused_at_mint(license_issuer):
    _, public = environment.delegation_keypair(create=True)
    parent = license_issuer.issue(environment_type="cluster", delegation_pubkey=public)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (Encoding, NoEncryption,
                                                              PrivateFormat)

    other = Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw,
                                                       NoEncryption())
    with pytest.raises(LicenseError):
        delegation.mint(parent, other)


def _resign(job, payload, seed):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    body = certificate.canonical_json(payload)
    sig = Ed25519PrivateKey.from_private_bytes(seed).sign(body)
    return f"{delegation.PREFIX}.{certificate.b64encode(body)}.{certificate.b64encode(sig)}"


def test_tampered_job_certificate_is_refused(cluster):
    parent, seed = cluster
    job = delegation.mint(parent, seed, ttl=3600)
    _, body, sig = job.split(".")
    payload = json.loads(certificate.b64decode(body))
    payload["expires_at"] += 30 * 86400
    forged = f"{delegation.PREFIX}.{certificate.b64encode(certificate.canonical_json(payload))}.{sig}"
    with pytest.raises(BadSignature):
        delegation.verify(forged)


def test_a_job_certificate_claiming_a_long_life_is_refused(cluster):
    parent, seed = cluster
    job = delegation.mint(parent, seed)
    payload = json.loads(certificate.b64decode(job.split(".")[1]))
    payload["expires_at"] = payload["issued_at"] + 30 * 86400
    with pytest.raises(BadSignature):
        delegation.verify(_resign(job, payload, seed))


def test_a_forged_parent_is_refused(cluster, license_issuer):
    parent, seed = cluster
    prefix, kid, body, sig = parent.split(".")
    bad_parent = ".".join([prefix, kid, body, sig[:-4] + "AAAA"])
    job = delegation.mint(parent, seed)
    payload = json.loads(certificate.b64decode(job.split(".")[1]))
    payload["parent"] = bad_parent
    with pytest.raises(BadSignature):
        delegation.verify(_resign(job, payload, seed))


def test_an_environment_certificate_in_the_job_variable_is_refused(license_issuer,
                                                                    monkeypatch):
    """Putting a bound desktop certificate in PLEXORA_LICENSE_JOB_CERT must not
    escape its binding."""
    cert = license_issuer.issue(env_binding="0" * 64)
    monkeypatch.setenv(store.ENV_JOB_CERT, cert)
    st = licensing.current()
    assert st.state == "invalid" and not st.paid


def test_a_server_signed_job_certificate_is_accepted(license_issuer, monkeypatch):
    now = int(time.time())
    cert = license_issuer.issue(environment_type="job", env_binding=None,
                                issued_at=now, expires_at=now + 86400)
    monkeypatch.setenv(store.ENV_JOB_CERT, cert)
    st = licensing.current()
    assert st.paid and st.source == "job"


def test_expired_job_certificate(cluster, monkeypatch):
    parent, seed = cluster
    job = delegation.mint(parent, seed, ttl=60)
    monkeypatch.setenv(store.ENV_JOB_CERT, job)
    from plexora.licensing import state

    monkeypatch.setattr(state, "_clock", lambda: time.time() + 120)
    assert licensing.current().state == "expired"


@pytest.mark.parametrize("text", ["PLEXORAD1", "PLEXORAD1.a.b.c", "PLEXORAD1.e30.AA",
                                  "PLEXORA2.e30.AA"])
def test_malformed_job_certificates(text):
    with pytest.raises((MalformedCertificate, BadSignature, LicenseError)):
        delegation.verify(text)


def test_lease_command(cluster, license_issuer, capsys):
    from plexora.licensing.cli import run

    parent, _ = cluster
    license_issuer.install(parent)
    lines = []
    assert run(["lease", "--ttl", "12h"], log=lines.append) == 0
    payload = delegation.verify(lines[0])
    assert payload["expires_at"] - payload["issued_at"] <= 12 * 3600
