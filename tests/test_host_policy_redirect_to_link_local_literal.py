"""A redirect to the metadata server's address is refused, not followed.

host_policy promises the link-local check "where the connection is actually
made -- every DNS answer and every redirect hop". A server on the lab network
(here, this machine) that answers 302 with `Location: http://169.254.169.254/...`
must not make the policy client open a connection to that address.
"""

import asyncio

import aiohttp
import pytest
from aiohttp import web

from plexora.server.utils import host_policy

METADATA = "169.254.169.254"


def _chain(exc):
    seen = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def test_a_redirect_to_a_link_local_address_is_refused_before_connecting(monkeypatch):
    monkeypatch.delenv("PLEXORA_ALLOW_LINK_LOCAL_URLS", raising=False)
    attempted = []
    original = aiohttp.TCPConnector._create_direct_connection

    async def recording(self, req, *args, **kwargs):
        attempted.append(req.host)
        if req.host == METADATA:
            # Never really dial out: the attempt itself is the defect.
            raise aiohttp.ClientConnectionError("test stopped a link-local dial")
        return await original(self, req, *args, **kwargs)

    monkeypatch.setattr(aiohttp.TCPConnector, "_create_direct_connection", recording)

    async def redirect(_request):
        raise web.HTTPFound(f"http://{METADATA}/latest/meta-data/iam/")

    async def scenario():
        app = web.Application()
        app.router.add_get("/x.zarr/.zattrs", redirect)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            session = await host_policy.get_client()
            try:
                async with session.get(f"http://127.0.0.1:{port}/x.zarr/.zattrs") as resp:
                    await resp.read()
            finally:
                await session.close()
        finally:
            await runner.cleanup()

    with pytest.raises(Exception) as caught:
        asyncio.run(scenario())

    assert METADATA not in attempted, (
        f"the policy client opened a connection to {METADATA} after a redirect "
        f"(hosts dialled: {attempted})")
    assert any(isinstance(e, host_policy.RefusedHost) for e in _chain(caught.value)), (
        f"expected RefusedHost, got {caught.value!r}")
