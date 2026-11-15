"""The primary asks a data node for gzip on every call (audit nr-providers-5).

urllib3 sends a pool's default headers only to a request that passes none, and
`providers.http.request` always passes its own (the token at least), so the
pool's `Accept-Encoding: gzip` never reached a node and a large JSON answer --
which the node compresses only when asked -- crossed the tunnel uncompressed.
"""
from types import SimpleNamespace

import pytest

from plexora.server.providers import http


class _Sent(Exception):
    pass


class _RecordingPool:
    def __init__(self):
        self.headers = None

    def request(self, method, url, **kw):
        self.headers = dict(kw.get("headers") or {})
        raise _Sent()


def _node():
    return SimpleNamespace(name="n", endpoint="http://127.0.0.1:1", token="t",
                           url=lambda path: "http://127.0.0.1:1" + path)


@pytest.mark.parametrize("extra", [None, {"If-None-Match": '"x"'}])
def test_request_asks_for_gzip(monkeypatch, extra):
    recorder = _RecordingPool()
    monkeypatch.setattr(http, "pool", lambda: recorder)
    with pytest.raises(_Sent):
        http.request(_node(), "GET", "/x", headers=extra, allow_disconnected=True)
    assert recorder.headers.get("Accept-Encoding") == "gzip"
    assert recorder.headers.get(http.TOKEN_HEADER) == "t"
    for key, value in (extra or {}).items():
        assert recorder.headers[key] == value


def test_caller_can_still_refuse_compression(monkeypatch):
    recorder = _RecordingPool()
    monkeypatch.setattr(http, "pool", lambda: recorder)
    with pytest.raises(_Sent):
        http.request(_node(), "GET", "/x", headers={"Accept-Encoding": "identity"},
                     allow_disconnected=True)
    assert recorder.headers["Accept-Encoding"] == "identity"
