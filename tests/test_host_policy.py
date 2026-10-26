"""What a web address may make the server fetch: not the metadata server."""

import asyncio

import pytest

from plexora.server.utils import host_policy, remote_store


@pytest.mark.parametrize("host", ["169.254.169.254", "metadata.google.internal",
                                  "fe80::1", "[fe80::1]", "0.0.0.0", "224.0.0.1",
                                  "::ffff:169.254.169.254", "METADATA.google.internal."])
def test_the_link_local_network_is_refused(host):
    with pytest.raises(host_policy.RefusedHost):
        host_policy.check_host(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "10.0.0.5", "192.168.1.20",
                                  "data.example.org", "::1"])
def test_this_machine_and_the_lab_network_stay_allowed(host):
    """Serving a zarr from `python -m http.server`, or a lab file server."""
    host_policy.check_host(host)


def test_the_rule_can_be_lifted(monkeypatch):
    monkeypatch.setenv("PLEXORA_ALLOW_LINK_LOCAL_URLS", "1")
    host_policy.check_host("169.254.169.254")


def test_opening_a_store_at_the_metadata_address_is_refused():
    with pytest.raises(host_policy.RefusedHost):
        remote_store.open_store("http://169.254.169.254/computeMetadata/v1/x.zarr")


def test_a_name_resolving_only_to_link_local_is_refused(monkeypatch):
    class Fake:
        async def resolve(self, host, port=0, family=0):
            return [{"host": "169.254.169.254", "port": port, "family": family,
                     "hostname": host, "proto": 0, "flags": 0}]

        async def close(self):
            pass

    import aiohttp.resolver
    monkeypatch.setattr(aiohttp.resolver, "DefaultResolver", Fake)
    resolver = host_policy._policy_resolver()
    with pytest.raises(host_policy.RefusedHost):
        asyncio.run(resolver.resolve("rebinding.example", 80))


def test_web_stores_are_opened_through_the_policy_client():
    options = remote_store.storage_options_for("https://data.example.org/x.zarr")
    assert options["get_client"] is host_policy.get_client


def test_a_redirect_hop_to_the_metadata_address_is_refused():
    class Url:
        host = "169.254.169.254"

    class Params:
        url = Url()

    with pytest.raises(host_policy.RefusedHost):
        asyncio.run(host_policy._on_request_start(None, None, Params()))
