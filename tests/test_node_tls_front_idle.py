"""The TLS front waits as long as the node computes (audit lead nr-node-5).

`TlsFront._pump` gave up on a connection after 300 s with no bytes either
way, even while Waitress was still computing the answer to a request on it.
Waitress already closes idle connections itself (`channel_timeout`, and only
when no request is in flight), which ends the pump through the backend's EOF.
"""
import types

from plexora.server.node import tls


class Socket:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []

    def recv(self, size):
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data):
        self.sent.append(data)

    def pending(self):
        return 0


def test_a_slow_answer_is_still_passed_on(monkeypatch):
    secure = Socket([])
    backend = Socket([b"HTTP/1.1 200 OK\r\n\r\n", b""])
    calls = []

    def select(readable, writable, errored, timeout=None):
        calls.append(timeout)
        if timeout is not None:
            return [], [], []  # the node took longer than the timeout
        return [backend], [], []

    monkeypatch.setattr(tls, "select", types.SimpleNamespace(select=select))
    tls.TlsFront._pump(secure, backend)

    assert secure.sent == [b"HTTP/1.1 200 OK\r\n\r\n"]
