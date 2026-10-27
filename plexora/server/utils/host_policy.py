"""Which machines a web address given to Plexora may make this process fetch.

An image or table can be opened from an http(s) address, and the server does
the fetching. On a cloud VM -- which Plexora provisions on Google Cloud --
there is one address on the machine's own link that answers with its service
account's credentials to anyone who asks from inside it: the metadata server
(169.254.169.254, `metadata.google.internal`). An address typed into an import
box, or a redirect from a server it names, must not be able to make Plexora ask
it.

So link-local, unspecified and multicast destinations are refused, checked
where the connection is actually made -- every DNS answer and every redirect
hop -- not only on the address as typed, since a name can resolve anywhere and
a redirect can point anywhere. Loopback and private networks stay allowed:
serving an OME-Zarr from `python -m http.server` on this machine, or from a lab
file server, is ordinary use. `PLEXORA_ALLOW_LINK_LOCAL_URLS=1` lifts the rule
for the rare site whose data really is on a link-local address.
"""

from __future__ import annotations

import ipaddress
import os
import socket

#: Names that mean "the machine's metadata server" without being an address.
METADATA_NAMES = frozenset({
    "metadata", "metadata.google.internal", "instance-data",
    "instance-data.ec2.internal", "metadata.azure.internal",
})


class RefusedHost(OSError):
    """The address leads somewhere a fetch on this machine's behalf may not go."""


def _lifted() -> bool:
    return os.environ.get("PLEXORA_ALLOW_LINK_LOCAL_URLS", "").lower() in ("1", "true", "yes")


def blocked_ip(value) -> bool:
    """Whether a destination IP is one no fetched URL may reach."""
    try:
        ip = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_link_local or ip.is_unspecified or ip.is_multicast


def check_host(host) -> None:
    """Raise RefusedHost for a host name or literal no fetch may reach."""
    if _lifted() or not host:
        return
    name = str(host).strip().strip("[]").rstrip(".").lower()
    if name in METADATA_NAMES or blocked_ip(name):
        raise RefusedHost(f"{host} is this machine's link-local network (where a "
                          f"cloud VM's metadata server answers); Plexora does not "
                          f"fetch from there.")


async def _on_request_start(_session, _context, params):
    check_host(params.url.host)


def _policy_resolver():
    import aiohttp
    from aiohttp.resolver import DefaultResolver

    class PolicyResolver(aiohttp.abc.AbstractResolver):
        """DNS answers with every refused address taken out."""

        def __init__(self):
            self._inner = DefaultResolver()

        async def resolve(self, host, port=0, family=socket.AF_INET):
            check_host(host)
            answers = await self._inner.resolve(host, port, family)
            if _lifted():
                return answers
            kept = [a for a in answers if not blocked_ip(a.get("host"))]
            if not kept:
                raise RefusedHost(f"{host} resolves only to link-local addresses; "
                                  f"Plexora does not fetch from there.")
            return kept

        async def close(self):
            await self._inner.close()

    return PolicyResolver()


async def get_client(**kwargs):
    """fsspec's `get_client` hook: an aiohttp session that obeys this policy."""
    import aiohttp

    trace = aiohttp.TraceConfig()
    trace.on_request_start.append(_on_request_start)
    connector = aiohttp.TCPConnector(resolver=_policy_resolver())
    return aiohttp.ClientSession(connector=connector,
                                 trace_configs=[*kwargs.pop("trace_configs", []), trace],
                                 **kwargs)
