"""urllib on the website: the browser's `fetch`, read as it arrives.

Python in the website's worker has no sockets. What calls out from it -- the
AI gateway (`biocognia.gateway`, `spatialagents.gateway`), a licence's
account lookup -- calls `urllib.request.urlopen`, and this module answers that
call with the page's own `fetch`, so the callers stay the standard library's.
pyodide_http did this with one synchronous XMLHttpRequest, which cost three
things here: a model's answer arrived whole at the end rather than token by
token, every status read as a success (a refused call parsed as an answer),
and the worker was held for the whole call, so no request of the page's was
answered meanwhile. This is `urlopen` over `fetch` instead:

- **Read as it arrives.** Where the browser has JSPI (Chrome), Python waits on
  `fetch` and then on each read of the body (`run_sync`), so a streamed answer
  is read line by line while it is still being sent.
- **The page is served meanwhile.** Each of those waits is handed to the
  worker (`install(wait_on=...)`), which answers the requests queued for
  Python in the gap, as it does while a job sleeps (`_work.pause`): the chat
  panel's polls, a gate the viewer reloads.
- **urllib's errors.** A status of 400 or more raises `HTTPError` with the
  body to read; a failure to reach the server raises `URLError`; nothing
  arriving for `timeout` seconds raises `URLError` before the answer began and
  `TimeoutError` after -- what `biocognia` and `spatialagents` catch.
- **Without JSPI** (Firefox, Safari): one synchronous XMLHttpRequest, the body
  whole, the same errors.

The User-Agent is never sent (Chrome ignores it; Firefox would send it and
make every call a preflight the platform's CORS does not admit), nor any
header a page may not set.

Under CPython nothing here runs unless a test hands it a stand-in for the
browser (`Fetcher`): tests/test_browser_fetch.py.
"""

from __future__ import annotations

import email.message
import http.client
import io
import urllib.error
import urllib.request

#: Request headers never sent: the browser's own, or one the platform's CORS
#: does not admit (User-Agent; see above).
DROPPED = frozenset(("user-agent", "host", "content-length", "connection", "accept-encoding",
                     "transfer-encoding", "keep-alive", "te", "trailer", "upgrade"))
#: What urllib waits by default (socket._GLOBAL_DEFAULT_TIMEOUT): no limit.
NO_TIMEOUT = 0.0

_wait_on = None
_installed = False


class Fetcher:
    """The browser's side of one call, behind three methods, so the rest of
    this module is plain Python (and tested as such):

    - `start(url, method, headers, body, timeout)` -> `(status, reason,
      [(name, value), ...], body_handle)` once the headers have arrived;
    - `read(body_handle, timeout)` -> the next bytes, or b"" at the end;
    - `close(body_handle)`: the caller stopped reading.

    They raise `TimeoutError` when nothing came in time and `OSError` when the
    server could not be reached or the answer broke off."""

    def start(self, url, method, headers, body, timeout):  # pragma: no cover - the browser's
        raise NotImplementedError

    def read(self, handle, timeout):  # pragma: no cover - the browser's
        raise NotImplementedError

    def close(self, handle):  # pragma: no cover - the browser's
        return None


class _Body(io.RawIOBase):
    """A response body read a chunk at a time from the fetcher."""

    def __init__(self, fetcher, handle, timeout):
        self._fetcher = fetcher
        self._handle = handle
        self._timeout = timeout
        self._chunk = b""
        self._at = 0
        self._ended = handle is None

    def readable(self):
        return True

    def readinto(self, buffer):
        if self._at >= len(self._chunk):
            if self._ended:
                return 0
            chunk = self._fetcher.read(self._handle, self._timeout)
            if not chunk:
                self._ended = True
                return 0
            self._chunk, self._at = bytes(chunk), 0
        view = memoryview(buffer)
        count = min(len(view), len(self._chunk) - self._at)
        view[:count] = self._chunk[self._at:self._at + count]
        self._at += count
        return count

    def close(self):
        if not self.closed and not self._ended:
            self._ended = True
            try:
                self._fetcher.close(self._handle)
            except Exception:  # noqa: BLE001 -- closing what is gone
                pass
        super().close()


class Response(io.BufferedReader):
    """What `urlopen` returns: read, readline and iterated by line like
    `http.client.HTTPResponse`, with its `status`, `reason`, `headers`,
    `url`, `getcode()`, `getheader()`, `info()` and `geturl()`."""

    def __init__(self, raw, *, status, reason, headers, url):
        super().__init__(raw, buffer_size=64 * 1024)
        self.status = self.code = int(status)
        self.reason = self.msg = reason or http.client.responses.get(self.status, "")
        self.headers = headers
        self.url = url

    def getcode(self):
        return self.status

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def getheaders(self):
        return list(self.headers.items())

    def info(self):
        return self.headers

    def geturl(self):
        return self.url

    def read1(self, size=-1):  # http.client's, for readers that ask for "what has come"
        return super().read1(size if size is not None else -1)


def _message(pairs) -> email.message.Message:
    message = http.client.HTTPMessage()
    for name, value in pairs:
        message[str(name)] = str(value)
    return message


def _timeout(value) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return NO_TIMEOUT
    return seconds if seconds > 0 else NO_TIMEOUT


def _request_parts(url, data):
    """(url, method, [(name, value)], body) as urllib would send them."""
    if isinstance(url, urllib.request.Request):
        request = url
        if data is not None:
            request.data = data
    else:
        request = urllib.request.Request(str(url), data=data)
    body = request.data
    if body is not None and not isinstance(body, (bytes, bytearray, memoryview)):
        if isinstance(body, str):
            raise TypeError("POST data should be bytes, an iterable of bytes, or a file object; "
                            "it cannot be of type str.")
        body = b"".join(bytes(part) for part in body)
    headers = [(name.title(), value) for name, value in request.header_items()]
    if body is not None and not any(name.lower() == "content-type" for name, _ in headers):
        headers.append(("Content-Type", "application/x-www-form-urlencoded"))
    headers = [(name, str(value)) for name, value in headers if name.lower() not in DROPPED]
    return request.full_url, request.get_method(), headers, (bytes(body) if body is not None else None)


def urlopen_with(fetcher: Fetcher, url, data=None, timeout=None, **_ignored) -> Response:
    """`urllib.request.urlopen(url, data, timeout)` through `fetcher`."""
    full_url, method, headers, body = _request_parts(url, data)
    seconds = _timeout(timeout)
    try:
        status, reason, pairs, handle = fetcher.start(full_url, method, headers, body, seconds)
    except TimeoutError as exc:
        raise urllib.error.URLError(TimeoutError("timed out")) from exc
    except OSError as exc:
        raise urllib.error.URLError(exc) from exc
    response = Response(_Body(fetcher, handle, seconds), status=status, reason=reason,
                        headers=_message(pairs), url=full_url)
    if status >= 400 or status == 0:
        raise urllib.error.HTTPError(full_url, status or 502, response.reason, response.headers, response)
    return response


# -- the browser ---------------------------------------------------------------------


class BrowserFetcher(Fetcher):
    """`fetch`, waited on through `wait_on` (or `run_sync`): needs JSPI."""

    def __init__(self, wait_on):
        self._wait = wait_on

    def _abortable(self, promise, controller, timeout):
        import js
        from pyodide.ffi import JsException

        timer = js.setTimeout(controller.abort.bind(controller), int(timeout * 1000)) if timeout else None
        try:
            return self._wait(promise)
        except JsException as exc:
            if getattr(exc, "name", "") == "AbortError" or controller.signal.aborted:
                raise TimeoutError("timed out") from None
            raise ConnectionError(str(getattr(exc, "message", exc)) or "the request failed") from None
        finally:
            if timer is not None:
                js.clearTimeout(timer)

    def start(self, url, method, headers, body, timeout):
        import js
        from pyodide.ffi import to_js

        controller = js.AbortController.new()
        init = {"method": method, "headers": to_js([[n, v] for n, v in headers]),
                "signal": controller.signal, "credentials": "omit", "cache": "no-store"}
        if body is not None:
            init["body"] = to_js(memoryview(body))
        response = self._abortable(js.fetch(url, to_js(init, dict_converter=js.Object.fromEntries)),
                                   controller, timeout)
        entries = getattr(js.Array, "from")(response.headers.entries())
        pairs = [(str(entry[0]), str(entry[1])) for entry in entries]
        handle = None
        if response.body is not None:
            handle = {"reader": response.body.getReader(), "controller": controller}
        return int(response.status), str(response.statusText or ""), pairs, handle

    def read(self, handle, timeout):
        result = self._abortable(handle["reader"].read(), handle["controller"], timeout)
        if result.done:
            return b""
        return bytes(result.value.to_py())

    def close(self, handle):
        try:
            handle["reader"].cancel()
        except Exception:  # noqa: BLE001 -- already done
            pass


class XhrFetcher(Fetcher):
    """One synchronous XMLHttpRequest (no JSPI): the body whole."""

    def start(self, url, method, headers, body, timeout):
        import js
        from pyodide.ffi import JsException, to_js

        xhr = js.XMLHttpRequest.new()
        xhr.open(method, url, False)
        xhr.responseType = "arraybuffer"
        if timeout:
            xhr.timeout = int(timeout * 1000)
        for name, value in headers:
            xhr.setRequestHeader(name, value)
        try:
            xhr.send(to_js(memoryview(body)) if body is not None else None)
        except JsException as exc:
            if getattr(exc, "name", "") == "TimeoutError":
                raise TimeoutError("timed out") from None
            raise ConnectionError(str(getattr(exc, "message", exc)) or "the request failed") from None
        if int(xhr.status) == 0:
            raise ConnectionError("the server could not be reached")
        pairs = []
        for line in str(xhr.getAllResponseHeaders() or "").split("\r\n"):
            name, sep, value = line.partition(":")
            if sep:
                pairs.append((name.strip(), value.strip()))
        data = bytes(xhr.response.to_py()) if xhr.response is not None else b""
        return int(xhr.status), str(xhr.statusText or ""), pairs, {"data": data}

    def read(self, handle, timeout):
        data, handle["data"] = handle["data"], b""
        return data


def _fetcher() -> Fetcher:
    try:
        from pyodide.ffi import can_run_sync, run_sync
    except ImportError:  # pragma: no cover - not a browser
        return XhrFetcher()
    if not can_run_sync():
        return XhrFetcher()
    return BrowserFetcher(_wait_on or run_sync)


def urlopen(url, data=None, timeout=None, *args, **kwargs) -> Response:
    """`urllib.request.urlopen` on the website."""
    return urlopen_with(_fetcher(), url, data, timeout)


def _opener_open(self, url, data=None, timeout=None):
    return urlopen(url, data, timeout)


def install(wait_on=None) -> bool:
    """urllib's `urlopen` (and `OpenerDirector.open`) through the browser, where
    there is one (`js` imports); `wait_on(promise)` waits for a promise while
    the page is served. True if installed."""
    global _wait_on, _installed
    try:
        import js  # noqa: F401
    except ImportError:
        return False
    _wait_on = wait_on
    urllib.request.urlopen = urlopen
    urllib.request.OpenerDirector.open = _opener_open
    _installed = True
    return True


def installed() -> bool:
    return _installed
