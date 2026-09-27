"""HTTP transport tokens: made once, stored as digests, verified, revoked."""

import os
import stat

from plexora.agent import Policy
from plexora.agent.tokens import TokenStore


def test_the_secret_is_returned_once_and_only_its_digest_stored(tmp_path):
    store = TokenStore()
    secret, record = store.create(scope="read", label="laptop")
    assert secret.startswith(f"plx_{record['id']}_") and "digest" not in record
    text = store.path.read_text()
    assert secret not in text and record["id"] in text
    assert store.path == tmp_path / ".agent" / "tokens.json"
    if os.name == "posix":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_verify_accepts_a_live_token_and_nothing_else(tmp_path):
    store = TokenStore()
    secret, record = store.create(scope="write")
    assert store.verify(secret)["scope"] == "write"
    assert store.verify(secret[:-1] + ("A" if secret[-1] != "A" else "B")) is None
    assert store.verify("plx_nothing_here") is None
    assert store.verify("not-a-token") is None
    assert store.revoke(record["id"]) is True
    assert store.verify(secret) is None
    assert store.count() == 0


def test_an_expired_token_is_refused(tmp_path):
    store = TokenStore()
    secret, record = store.create(scope="read", expires_days=1e-9)
    assert store.verify(secret) is None
    assert store.list()[0]["id"] == record["id"]


def test_listing_shows_no_secrets(tmp_path):
    store = TokenStore()
    secret, _ = store.create(scope="admin")
    listed = store.list()
    assert all("digest" not in r for r in listed)
    assert secret not in repr(listed)


def test_a_scope_only_narrows_the_server_policy():
    base = Policy(allow_source_writes=True, allow_destructive=True)
    read = base.narrowed_by_scope("read", principal="token:a")
    assert (read.allow_writes, read.allow_source_writes, read.allow_destructive) == \
        (False, False, False)
    assert read.principal == "token:a" and read.egress == base.egress
    write = base.narrowed_by_scope("write")
    assert write.allow_writes and not write.allow_source_writes and not write.allow_destructive
    assert base.narrowed_by_scope("admin") == base
    # Admin cannot widen a server that allows nothing.
    assert Policy().narrowed_by_scope("admin").allow_source_writes is False
    assert base.narrowed_by_scope("bogus").allow_writes is False


def test_the_token_command(tmp_path):
    from plexora.ai.setup import token_command

    printed = []
    assert token_command("create", scope="read", label="hpc", out=printed.append) == 0
    secret = printed[1].strip()
    assert secret.startswith("plx_") and f"PLEXORA_MCP_TOKEN={secret}" in printed[-1]
    listed = []
    token_command("list", out=listed.append)
    assert "read" in listed[0] and "hpc" in listed[0] and secret not in listed[0]
    token_id = listed[0].split()[0]
    assert token_command("revoke", token_id=token_id, out=listed.append) == 0
    assert token_command("revoke", token_id="nope", out=listed.append) == 1
