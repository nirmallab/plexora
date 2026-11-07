"""urllib on the website (plexora/browser/fetch.py), with a stand-in for the
browser's `fetch`.

What is pinned: an answer is read as it arrives -- a streamed model call's
deltas reach `on_delta` while the stream is still being read, not at its end
-- a refusal raises urllib's `HTTPError` with the body the gateway sent (so
`biocognia` reads its code), a server that cannot be reached is `URLError`,
nothing arriving in time is `URLError` before the answer and `TimeoutError`
mid-answer (spatialagents' `interrupted`), and the User-Agent is never sent.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from plexora.browser import fetch


class FakeFetcher(fetch.Fetcher):
    """The browser, as a script: an answer's status, headers and chunks, and
    a log of what was asked and read, in order."""

    def __init__(self, status=200, headers=(("Content-Type", "application/json"),), chunks=(b"{}",),
                 fail_start=None, fail_read_at=None):
        self.status, self.headers, self.chunks = status, list(headers), list(chunks)
        self.fail_start, self.fail_read_at = fail_start, fail_read_at
        self.log = []
        self.asked = None
        self.closed = False

    def start(self, url, method, headers, body, timeout):
        self.asked = {"url": url, "method": method, "headers": dict(headers), "body": body, "timeout": timeout}
        self.log.append("start")
        if self.fail_start is not None:
            raise self.fail_start
        return self.status, "", self.headers, {"left": list(self.chunks)}

    def read(self, handle, timeout):
        if self.fail_read_at is not None and len(self.chunks) - len(handle["left"]) == self.fail_read_at:
            raise self.fail_read_at_error
        chunk = handle["left"].pop(0) if handle["left"] else b""
        self.log.append(f"read:{len(chunk)}")
        return chunk

    def close(self, handle):
        self.closed = True


def _sse(*events):
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def test_a_json_answer_reads_like_urllibs():
    fake = FakeFetcher(chunks=[b'{"a": ', b"1}"])
    request = urllib.request.Request("https://gw.test/v1/ai/balance", method="GET",
                                     headers={"User-Agent": "plexora/1 (ai)", "Authorization": "Bearer t",
                                              "BioCognia-Client": "plexora/1"})
    with fetch.urlopen_with(fake, request, timeout=60) as response:
        assert response.status == 200 and response.getcode() == 200
        assert response.headers.get("content-type") == "application/json"
        assert json.loads(response.read(4 * 1024 * 1024)) == {"a": 1}
    assert fake.asked["method"] == "GET" and fake.asked["timeout"] == 60
    assert "User-Agent" not in fake.asked["headers"]
    assert fake.asked["headers"]["Authorization"] == "Bearer t"
    assert fake.asked["headers"]["Biocognia-Client"] == "plexora/1"


def test_a_body_is_sent_with_urllibs_default_type():
    fake = FakeFetcher()
    fetch.urlopen_with(fake, "https://gw.test/x", data=b"a=1").read()
    assert fake.asked["method"] == "POST" and fake.asked["body"] == b"a=1"
    assert fake.asked["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_a_refusal_is_an_httperror_with_the_gateways_body():
    from biocognia.gateway import error_from

    body = json.dumps({"error": {"code": "insufficient_credits", "message": "no credit"}}).encode()
    fake = FakeFetcher(status=402, chunks=[body])
    with pytest.raises(urllib.error.HTTPError) as caught:
        fetch.urlopen_with(fake, "https://gw.test/v1/ai/messages", data=b"{}", timeout=5)
    assert caught.value.code == 402 and caught.value.fp is not None
    assert error_from(caught.value.code, caught.value.read(64 * 1024)).code == "insufficient_credits"


def test_no_server_and_no_answer_are_urllibs_errors():
    with pytest.raises(urllib.error.URLError) as unreachable:
        fetch.urlopen_with(FakeFetcher(fail_start=ConnectionError("Failed to fetch")), "https://gw.test/x")
    assert isinstance(unreachable.value.reason, OSError)
    with pytest.raises(urllib.error.URLError) as silent:
        fetch.urlopen_with(FakeFetcher(fail_start=TimeoutError("timed out")), "https://gw.test/x", timeout=1)
    assert isinstance(silent.value.reason, TimeoutError)


def test_a_stream_is_read_as_it_arrives_and_a_silence_mid_answer_is_a_timeout():
    deltas = [("content_block_delta", {"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "text_delta", "text": word}})
              for word in ("one ", "two ", "three")]
    head = _sse(("biocognia.accepted", {"gateway_request_id": "r1"}),
                ("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}}))
    # Each delta in a chunk of its own, the second split mid-line.
    second = _sse(deltas[1])
    chunks = [head, _sse(deltas[0]), second[:17], second[17:], _sse(deltas[2])]
    fake = FakeFetcher(headers=[("Content-Type", "text/event-stream")], chunks=chunks)
    seen = []
    with fetch.urlopen_with(fake, "https://gw.test/v1/ai/messages", data=b"{}", timeout=300) as response:
        from biocognia.gateway import iter_sse

        for event, data in iter_sse(response):
            if event == "content_block_delta":
                seen.append((json.loads(data)["delta"]["text"], len(fake.log)))
    assert [text for text, _ in seen] == ["one ", "two ", "three"]
    # Each delta was handed on before the chunks after it were read.
    assert [at for _, at in seen] == sorted(at for _, at in seen) and seen[0][1] < len(fake.log) - 1

    broken = FakeFetcher(headers=[("Content-Type", "text/event-stream")], chunks=chunks)
    broken.fail_read_at, broken.fail_read_at_error = 2, TimeoutError("timed out")
    with pytest.raises(TimeoutError):
        with fetch.urlopen_with(broken, "https://gw.test/v1/ai/messages", data=b"{}", timeout=300) as response:
            for _ in response:
                pass


def test_spatialagents_streams_its_model_call_through_it(monkeypatch):
    """The harness's own call over the stand-in: the deltas reach `on_delta`
    one by one while the stream is being read, and the answer is whole."""
    from spatialagents.gateway import GatewayClient
    from spatialagents.testing import fixed_tokens
    from spatialagents.wire import ModelRequest

    events = [("biocognia.accepted", {"gateway_request_id": "r1", "billing": "credits"}),
              ("message_start", {"type": "message_start", "message": {"id": "m", "usage": {}}}),
              ("content_block_start", {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "text", "text": ""}})]
    events += [("content_block_delta", {"type": "content_block_delta", "index": 0,
                                        "delta": {"type": "text_delta", "text": w}}) for w in ("He", "llo")]
    events += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
               ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                  "usage": {"output_tokens": 2}}),
               ("message_stop", {"type": "message_stop"}),
               ("biocognia.usage", {"gateway_request_id": "r1", "status": "ok", "usage": {},
                                    "price_micro": 1, "charged_micro": 1, "billing": "credits"})]
    fake = FakeFetcher(headers=[("Content-Type", "text/event-stream")], chunks=[_sse(e) for e in events])
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: fetch.urlopen_with(fake, *a, **k))
    monkeypatch.delenv("BIOCOGNIA_OFFLINE", raising=False)
    client = GatewayClient(fixed_tokens("plexora"), "http://127.0.0.1:9", sleep=lambda s: None)
    heard = []
    response = client.messages(ModelRequest(capability="text", system=[], messages=[
        {"role": "user", "content": [{"type": "text", "text": "hi"}]}], max_tokens=10, task="chat.turn"),
        idempotency_key="k1", on_delta=lambda piece: heard.append((piece, len(fake.log))))
    assert response.text == "Hello"
    assert [piece for piece, _ in heard] == ["He", "llo"]
    assert heard[0][1] < heard[1][1] < len(fake.log)            # handed on as they were read
    assert "User-Agent" not in fake.asked["headers"] and fake.asked["headers"]["Idempotency-Key"] == "k1"


def test_install_is_the_browsers_only():
    # Under CPython there is no `js`: nothing is replaced.
    before = urllib.request.urlopen
    assert fetch.install() is False
    assert urllib.request.urlopen is before
