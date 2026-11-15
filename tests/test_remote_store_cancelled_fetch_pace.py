"""A cancelled fetch still ends its read_pace bracket (audit lead io-remote-4).

`ChunkCacheStore._fetch` called `read_pace.began(host)` and caught only
Exception, and asyncio.CancelledError is a BaseException: a cancelled fetch
left the host counted as in flight for the rest of the process.
"""
import asyncio

import pytest

from plexora.server.utils import read_pace, remote_store

ROOT = "s3://audit-bucket/cancelled.zarr"


class Cancelled:
    async def get(self, key, prototype, byte_range=None):
        raise asyncio.CancelledError()


def test_a_cancelled_fetch_is_not_in_flight_for_ever():
    read_pace.set_reporter(lambda event: None)
    try:
        store = remote_store.ChunkCacheStore(Cancelled(), ROOT)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(store._fetch("0/c/0/0", None))
        state = read_pace._hosts[("network", remote_store.host_of(ROOT))]
        assert state["inflight"] == 0
    finally:
        read_pace.set_reporter(None)
