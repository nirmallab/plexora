"""Buckets and web hosts on the website: `s3://`, `gs://` and `https://`
read through the page's own `fetch`.

The desktop reads a web address with fsspec over aiohttp, s3fs or gcsfs
(`server/utils/remote_store.py`), and none of them runs in the website's
worker: Python there has no sockets. Opening `s3://...` on the website said
"Reading s3:// addresses needs the s3fs package". This module is the
filesystem fsspec finds instead, registered for those schemes by the boot
(`install`), so `remote_store` -- its chunk cache, `RemoteFile`, the listings
New Dataset walks -- runs unchanged on top of it.

What each address becomes:

- **`s3://bucket/key`**: `https://bucket.s3.amazonaws.com/key` (the region's
  host when the address book names one, path-style under an `endpoint_url`
  for an S3-compatible store). A key is read with a `Range` GET and listed
  with ListObjectsV2 (`?list-type=2`). A file's size comes from that listing,
  never from HEAD: a public bucket's CORS rules commonly admit GET only
  (`lin-2023-orion-crc` answers HEAD with no CORS header, and exposes neither
  `Content-Range` nor `ETag`). A bucket that will not list is asked for the
  object itself, its headers read and the body left unread.
- **`gs://bucket/key`**: Google's JSON API, for listing (`objects.list`), a
  file's size and version (`objects.get`) and its bytes (`alt=media` with
  `Range`). The JSON API answers a page whatever the bucket's CORS settings;
  the XML host (`storage.googleapis.com/bucket/key`) does not, so IDC's
  `idc-open-data` cannot be read from a page through it.
- **`https://...`**: the address itself, `Range` GETs; its size from HEAD, or
  from a GET's headers where HEAD is refused; a folder listed from the links
  of its index page, as fsspec's own HTTP filesystem lists one.

Every request is anonymous (`credentials: "omit"`): the website reads public
buckets. A host the page may not read (no CORS) fails as `fetch` fails, with no
reason given; `PageRefused` says which it was when the host can be shown to
answer (a `no-cors` request to it succeeds), so the person reads "does not let
web pages read it" rather than "cannot be reached".

**Waiting.** Where the coroutine runs on Pyodide's own loop (with JSPI zarr's
synchronous API waits there, and so does a TIFF's header read), each request
is the page's `fetch`, awaited: requests issued together (`asyncio.gather`,
remote_store's `read_ranges`) are in flight together. On any other loop (a
browser without JSPI, or inside `no_suspend.held()` -- h5py reading a remote
.ims -- where `boot.blocking_loop` runs coroutines on a loop of its own that
cannot wait for a promise) a request is one synchronous
XMLHttpRequest, which a worker may make.

Under CPython nothing here touches a browser unless a test installs it with a
stand-in `Transport` (tests/test_browser_buckets.py).
"""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ElementTree
from typing import Optional
from urllib.parse import quote, urlencode, urljoin, urlsplit

from fsspec.asyn import AsyncFileSystem

#: The schemes this module reads, as fsspec names them.
SCHEMES = ("s3", "s3a", "gs", "gcs", "http", "https")
#: The same, as `remote_store` names them (its canonical spellings).
CANONICAL = frozenset(("s3", "gs", "http", "https"))
#: A request that has not answered in this long fails (remote_store retries
#: and then says the host cannot be reached), as the desktop's read timeout.
TIMEOUT_S = 90.0
#: Google's JSON API, which answers pages whatever a bucket's CORS settings.
GCS_API = "https://storage.googleapis.com/storage/v1"
#: The fields an object's record is asked for (size and version).
GCS_FIELDS = "name,size,etag,generation,updated,md5Hash,crc32c"
#: Keys a listing asks for in one request.
PAGE_SIZE = 1000

_installed: Optional["Transport"] = None


# -- the requests ------------------------------------------------------------


class Answer:
    """One response: `status`, `headers` (names lower-cased) and `body`."""

    def __init__(self, status: int, headers: dict, body: bytes = b""):
        self.status = int(status)
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self.body = bytes(body or b"")

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class PageUnreachable(ConnectionError):
    """`fetch` failed: no network, or a host that does not let pages read it."""


class PageRefused(PermissionError):
    """A host that answers, but not to a web page: its CORS settings do not
    let this site read it. Read as "forbidden" by remote_store, never retried."""

    page_refused = True


class StatusError(OSError):
    """A response that is neither the bytes asked for nor "not there"."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)


class Transport:
    """The browser's side, behind two coroutines, so the rest of this module is
    plain Python (and tested as such).

    - `request(method, url, headers, body=True, timeout=...)` -> `Answer`.
      `body=False`: the headers are what is wanted, the body is left unread
      (`stops_early()` says whether it can be, here and now).
    - `reaches(url)` -> True when the host answers at all (a `no-cors`
      request), False when it does not, None when that cannot be asked.

    `request` raises `PageUnreachable` when nothing came back and
    `TimeoutError` when nothing came back in time."""

    async def request(self, method, url, headers=None, *, body=True, timeout=TIMEOUT_S) -> Answer:
        raise NotImplementedError  # pragma: no cover - the browser's

    def stops_early(self) -> bool:
        return True

    async def reaches(self, url) -> Optional[bool]:
        return None


def _on_page_loop() -> bool:
    """Whether the running loop is Pyodide's, the one a promise resolves on."""
    try:
        from pyodide.webloop import WebLoop
    except ImportError:  # pragma: no cover - not a browser
        return False
    try:
        return isinstance(asyncio.get_running_loop(), WebLoop)
    except RuntimeError:
        return False


class BrowserTransport(Transport):
    """`fetch` on Pyodide's loop, a synchronous XMLHttpRequest anywhere else."""

    def stops_early(self) -> bool:
        return _on_page_loop()

    async def request(self, method, url, headers=None, *, body=True, timeout=TIMEOUT_S) -> Answer:
        if _on_page_loop():
            return await self._fetch(method, url, headers or {}, body, timeout)
        return self._xhr(method, url, headers or {}, body, timeout)

    async def _fetch(self, method, url, headers, body, timeout):
        import js
        from pyodide.ffi import JsException, create_once_callable, to_js

        controller = js.AbortController.new()
        expired = []

        def expire():
            expired.append(True)
            controller.abort()

        timer = js.setTimeout(create_once_callable(expire), int(timeout * 1000)) if timeout else None
        init = {"method": method, "headers": to_js([[n, v] for n, v in headers.items()]),
                "signal": controller.signal, "credentials": "omit", "cache": "no-store",
                "mode": "cors", "redirect": "follow"}
        try:
            response = await js.fetch(url, to_js(init, dict_converter=js.Object.fromEntries))
            pairs = {str(e[0]): str(e[1]) for e in getattr(js.Array, "from")(response.headers.entries())}
            data = b""
            if body and method != "HEAD":
                data = (await response.arrayBuffer()).to_bytes()
            elif response.body is not None:
                controller.abort()  # the headers were all that was wanted
            return Answer(int(response.status), pairs, data)
        except JsException as exc:
            if expired:
                raise TimeoutError(f"{_host(url)} did not answer within {timeout:.0f} s") from None
            raise PageUnreachable(str(getattr(exc, "message", exc)) or "the request failed") from None
        finally:
            if timer is not None:
                js.clearTimeout(timer)

    def _xhr(self, method, url, headers, body, timeout):
        import js
        from pyodide.ffi import JsException

        xhr = js.XMLHttpRequest.new()
        xhr.open(method, url, False)
        xhr.responseType = "arraybuffer"
        if timeout:
            xhr.timeout = int(timeout * 1000)
        for name, value in headers.items():
            xhr.setRequestHeader(name, value)
        try:
            xhr.send(None)
        except JsException as exc:
            if getattr(exc, "name", "") == "TimeoutError":
                raise TimeoutError(f"{_host(url)} did not answer within {timeout:.0f} s") from None
            raise PageUnreachable(str(getattr(exc, "message", exc)) or "the request failed") from None
        if int(xhr.status) == 0:
            raise PageUnreachable("the request failed")
        pairs = {}
        for line in str(xhr.getAllResponseHeaders() or "").split("\r\n"):
            name, sep, value = line.partition(":")
            if sep:
                pairs[name.strip()] = value.strip()
        data = bytes(xhr.response.to_py()) if (body and xhr.response is not None) else b""
        return Answer(int(xhr.status), pairs, data)

    async def reaches(self, url) -> Optional[bool]:
        if not _on_page_loop():
            return None
        import js
        from pyodide.ffi import JsException, to_js

        init = {"method": "HEAD", "mode": "no-cors", "credentials": "omit", "cache": "no-store"}
        try:
            await js.fetch(url, to_js(init, dict_converter=js.Object.fromEntries))
        except JsException:
            return False
        return True


# -- shared --------------------------------------------------------------------


def _host(url) -> str:
    return urlsplit(str(url)).netloc or str(url)


def _range_header(start, end) -> Optional[str]:
    """fsspec's `[start, end)` (start < 0 counts from the end) as a Range."""
    if start is None and end is None:
        return None
    start = 0 if start is None else int(start)
    if start < 0:
        return f"bytes={start}"
    if end is None:
        return f"bytes={start}-" if start else None
    return f"bytes={start}-{int(end) - 1}"


class PageFileSystem(AsyncFileSystem):
    """What the three have in common: reads, errors, the transport.

    Always asynchronous: fsspec's synchronous API would start its IO thread,
    and the worker has no threads. zarr's FsspecStore and remote_store call
    only the coroutines."""

    async_impl = True
    root_marker = ""

    def __init__(self, *args, anon=True, endpoint_url=None, region=None, client_kwargs=None,
                 asynchronous=True, loop=None, **kwargs):
        # The desktop's options (s3fs's config_kwargs and profile, gcsfs's
        # token) are taken and ignored: every request here is anonymous.
        for name in ("config_kwargs", "profile", "token", "project", "get_client", "trust_env"):
            kwargs.pop(name, None)
        super().__init__(*args, asynchronous=True, loop=None, **kwargs)
        client_kwargs = dict(client_kwargs or {})
        self.anon = bool(anon)
        self.endpoint_url = endpoint_url or client_kwargs.get("endpoint_url")
        self.region = region or client_kwargs.get("region_name")

    @property
    def transport(self) -> Transport:
        if _installed is None:
            raise RuntimeError("plexora.browser.buckets is not installed")
        return _installed

    async def _get(self, url, headers=None, *, body=True) -> Answer:
        try:
            return await self.transport.request("GET", url, headers, body=body)
        except PageUnreachable as exc:
            await self._refused(url, exc)
            raise

    async def _refused(self, url, exc):
        """`PageRefused` in place of a failed request when the host does answer."""
        if await self.transport.reaches(url):
            raise PageRefused(
                f"{_host(url)} does not let web pages read it (its CORS settings), so "
                "the website cannot open it. Open it in the desktop app, or pair a "
                "node that can reach it.") from exc

    @staticmethod
    def _fail(answer: Answer, what: str):
        status = answer.status
        if status == 404 or status == 410:
            raise FileNotFoundError(what)
        if status in (401, 403):
            raise PermissionError(f"{what}: refused ({status}); the website reads public buckets only")
        if status == 416:
            raise StatusError(416, f"{what}: range not satisfiable")
        raise StatusError(status, f"{what}: the host answered {status}")

    async def _read(self, url, what, start=None, end=None, size=None) -> bytes:
        """`[start, end)` of the object at `url`."""
        if (end is not None and int(end) < 0) or (start is not None and int(start) < 0 and end is not None):
            if size is None:
                size = (await self._info(what))["size"]
            start = (size + int(start)) if start is not None and int(start) < 0 else (start or 0)
            end = size + int(end) if int(end) < 0 else int(end)
        if start is not None and end is not None and int(start) >= 0 and int(end) <= int(start):
            return b""
        header = _range_header(start, end)
        answer = await self._get(url, {"Range": header} if header else None)
        if answer.status == 206:
            return answer.body
        if answer.status == 200:
            if header is None:
                return answer.body
            # A host that ignores Range sent the whole object.
            begin = int(start or 0)
            return answer.body[begin:] if end is None else answer.body[begin:int(end)]
        self._fail(answer, what)

    async def _exists(self, path, **kwargs):
        try:
            await self._info(path)
            return True
        except (FileNotFoundError, PermissionError):
            return False

    def _split(self, path) -> tuple[str, str]:
        path = self._strip_protocol(path)
        bucket, _, key = path.partition("/")
        return bucket, key.strip("/")


# -- s3:// ---------------------------------------------------------------------


def _xml_children(node, name):
    return [child for child in node if child.tag.rsplit("}", 1)[-1] == name]


def _xml_text(node, name, default=None):
    found = _xml_children(node, name)
    return found[0].text if found and found[0].text is not None else default


class S3PageFileSystem(PageFileSystem):
    protocol = ("s3", "s3a")

    def _base(self, bucket) -> str:
        if self.endpoint_url:
            return f"{str(self.endpoint_url).rstrip('/')}/{bucket}"
        host = f"s3.{self.region}.amazonaws.com" if self.region else "s3.amazonaws.com"
        if "." in bucket:  # a dotted name breaks the wildcard certificate
            return f"https://{host}/{bucket}"
        return f"https://{bucket}.{host}"

    def _url(self, bucket, key) -> str:
        return f"{self._base(bucket)}/{quote(key, safe='/-_.~')}"

    async def _list(self, bucket, prefix, *, limit=None):
        """([object record], [folder name]) directly under `prefix`."""
        files, folders, token = [], [], None
        while True:
            params = {"list-type": "2", "prefix": prefix, "delimiter": "/",
                      "max-keys": str(min(limit or PAGE_SIZE, PAGE_SIZE))}
            if token:
                params["continuation-token"] = token
            answer = await self._get(f"{self._base(bucket)}/?{urlencode(params)}")
            if answer.status != 200:
                self._fail(answer, f"s3://{bucket}/{prefix}")
            root = ElementTree.fromstring(answer.body)
            for item in _xml_children(root, "Contents"):
                files.append({
                    "name": f"{bucket}/{_xml_text(item, 'Key', '')}",
                    "type": "file",
                    "size": int(_xml_text(item, "Size", "0")),
                    "ETag": _xml_text(item, "ETag"),
                    "LastModified": _xml_text(item, "LastModified"),
                })
            for item in _xml_children(root, "CommonPrefixes"):
                folders.append(f"{bucket}/{_xml_text(item, 'Prefix', '')}".rstrip("/"))
            token = _xml_text(root, "NextContinuationToken")
            truncated = (_xml_text(root, "IsTruncated", "false") or "").lower() == "true"
            if not truncated or not token or (limit and len(files) + len(folders) >= limit):
                return files, folders

    async def _info(self, path, **kwargs):
        bucket, key = self._split(path)
        if not key:
            return {"name": bucket, "type": "directory", "size": 0}
        name = f"{bucket}/{key}"
        try:
            # The key sorts first among its prefix, its folder ("key/") next.
            files, folders = await self._list(bucket, key, limit=3)
        except PageRefused:
            raise
        except PermissionError:
            return await self._info_from_object(bucket, key)
        for record in files:
            if record["name"] == name:
                return record
        if name in folders:
            return {"name": name, "type": "directory", "size": 0}
        raise FileNotFoundError(f"s3://{name}")

    async def _info_from_object(self, bucket, key):
        """A bucket that will not list: the object's own headers."""
        name = f"{bucket}/{key}"
        url = self._url(bucket, key)
        if not self.transport.stops_early():
            # Not a whole-object GET to learn a length: read one byte and
            # take what the headers say.
            answer = await self._get(url, {"Range": "bytes=0-0"})
            if answer.status not in (200, 206):
                self._fail(answer, f"s3://{name}")
            total = answer.headers.get("content-range", "").rpartition("/")[2]
            size = int(total) if total.isdigit() else None
        else:
            answer = await self._get(url, body=False)
            if answer.status not in (200, 206):
                self._fail(answer, f"s3://{name}")
            length = answer.headers.get("content-length")
            size = int(length) if length and length.isdigit() else None
        return {"name": name, "type": "file", "size": size,
                "ETag": answer.headers.get("etag"),
                "LastModified": answer.headers.get("last-modified")}

    async def _ls(self, path, detail=True, **kwargs):
        bucket, key = self._split(path)
        files, folders = await self._list(bucket, f"{key}/" if key else "")
        own = f"{bucket}/{key}/" if key else None
        entries = [record for record in files if record["name"] != own]
        entries += [{"name": folder, "type": "directory", "size": 0} for folder in folders]
        if not entries:
            info = await self._info(path)  # a file is its own listing; nothing raises
            entries = [info]
        return entries if detail else [entry["name"] for entry in entries]

    async def _cat_file(self, path, start=None, end=None, **kwargs):
        bucket, key = self._split(path)
        return await self._read(self._url(bucket, key), f"s3://{bucket}/{key}", start, end)


# -- gs:// ---------------------------------------------------------------------


class GCSPageFileSystem(PageFileSystem):
    protocol = ("gs", "gcs")

    @staticmethod
    def _object(bucket, key) -> str:
        return f"{GCS_API}/b/{quote(bucket, safe='')}/o/{quote(key, safe='')}"

    @staticmethod
    def _record(bucket, item) -> dict:
        return {
            "name": f"{bucket}/{item.get('name', '')}",
            "type": "file",
            "size": int(item.get("size") or 0),
            "etag": item.get("etag"),
            "generation": item.get("generation"),
            "updated": item.get("updated"),
            "md5Hash": item.get("md5Hash"),
            "crc32c": item.get("crc32c"),
        }

    async def _list(self, bucket, prefix, *, limit=None):
        files, folders, token = [], [], None
        while True:
            params = {"prefix": prefix, "delimiter": "/",
                      "maxResults": str(min(limit or PAGE_SIZE, PAGE_SIZE)),
                      "fields": f"items({GCS_FIELDS}),prefixes,nextPageToken"}
            if token:
                params["pageToken"] = token
            answer = await self._get(f"{GCS_API}/b/{quote(bucket, safe='')}/o?{urlencode(params)}")
            if answer.status != 200:
                self._fail(answer, f"gs://{bucket}/{prefix}")
            found = json.loads(answer.body or b"{}")
            files += [self._record(bucket, item) for item in found.get("items") or []]
            folders += [f"{bucket}/{p}".rstrip("/") for p in found.get("prefixes") or []]
            token = found.get("nextPageToken")
            if not token or (limit and len(files) + len(folders) >= limit):
                return files, folders

    async def _info(self, path, **kwargs):
        bucket, key = self._split(path)
        if not key:
            return {"name": bucket, "type": "directory", "size": 0}
        answer = await self._get(f"{self._object(bucket, key)}?fields={GCS_FIELDS}")
        if answer.status == 200:
            return self._record(bucket, json.loads(answer.body))
        if answer.status != 404:
            self._fail(answer, f"gs://{bucket}/{key}")
        files, folders = await self._list(bucket, f"{key}/", limit=1)
        if files or folders:
            return {"name": f"{bucket}/{key}", "type": "directory", "size": 0}
        raise FileNotFoundError(f"gs://{bucket}/{key}")

    async def _ls(self, path, detail=True, **kwargs):
        bucket, key = self._split(path)
        files, folders = await self._list(bucket, f"{key}/" if key else "")
        own = f"{bucket}/{key}/" if key else None
        entries = [record for record in files if record["name"] != own]
        entries += [{"name": folder, "type": "directory", "size": 0} for folder in folders]
        if not entries:
            entries = [await self._info(path)]
        return entries if detail else [entry["name"] for entry in entries]

    async def _cat_file(self, path, start=None, end=None, **kwargs):
        bucket, key = self._split(path)
        return await self._read(f"{self._object(bucket, key)}?alt=media", f"gs://{bucket}/{key}",
                                start, end)


# -- https:// ------------------------------------------------------------------

#: A link in an index page, as fsspec's HTTP filesystem finds them.
_LINK = re.compile(r"""<(?:a|A)\s+(?:[^>]*?\s+)?(?:href|HREF)=["'](?P<url>[^"'#?]+)""")


class HTTPPageFileSystem(PageFileSystem):
    protocol = ("http", "https")

    @classmethod
    def _strip_protocol(cls, path):
        """The address itself, as fsspec's HTTP filesystem keeps it."""
        text = str(path)
        parts = urlsplit(text)
        if parts.path not in ("", "/"):
            text = text.rstrip("/")
        return text

    @classmethod
    def _parent(cls, path):
        text = cls._strip_protocol(path)
        parts = urlsplit(text)
        if parts.path in ("", "/"):
            return text
        return text.rsplit("/", 1)[0]

    async def _info(self, path, **kwargs):
        url = self._strip_protocol(path)
        answer = None
        try:
            answer = await self.transport.request("HEAD", url, body=False)
        except PageUnreachable:
            answer = None  # HEAD not admitted: the GET below says what is there
        if answer is None or answer.status in (403, 405, 501):
            if self.transport.stops_early():
                answer = await self._get(url, body=False)
            else:
                answer = await self._get(url, {"Range": "bytes=0-0"})
        if answer.status not in (200, 206):
            self._fail(answer, url)
        size = None
        if answer.status == 206:
            total = answer.headers.get("content-range", "").rpartition("/")[2]
            size = int(total) if total.isdigit() else None
        elif "content-encoding" not in answer.headers:
            length = answer.headers.get("content-length", "")
            size = int(length) if length.isdigit() else None
        return {"name": url, "type": "file", "size": size,
                "ETag": answer.headers.get("etag"),
                "LastModified": answer.headers.get("last-modified")}

    async def _ls(self, path, detail=True, **kwargs):
        url = self._strip_protocol(path)
        answer = await self._get(url + "/" if not url.endswith("/") else url)
        if answer.status != 200:
            self._fail(answer, url)
        base = url.rstrip("/") + "/"
        seen, entries = set(), []
        for match in _LINK.finditer(answer.text()):
            target = urljoin(base, match.group("url"))
            if not target.startswith(base) or target == base:
                continue
            name = target.rstrip("/")
            if name in seen:
                continue
            seen.add(name)
            entries.append({"name": name, "size": None,
                            "type": "directory" if target.endswith("/") else "file"})
        return entries if detail else [entry["name"] for entry in entries]

    async def _cat_file(self, path, start=None, end=None, **kwargs):
        url = self._strip_protocol(path)
        return await self._read(url, url, start, end)


# -- the registration ----------------------------------------------------------

FILESYSTEMS = {"s3": S3PageFileSystem, "s3a": S3PageFileSystem,
               "gs": GCSPageFileSystem, "gcs": GCSPageFileSystem,
               "http": HTTPPageFileSystem, "https": HTTPPageFileSystem}


def install(transport: Optional[Transport] = None) -> bool:
    """Make fsspec read `SCHEMES` through `transport` (the browser's, where
    there is one: `js` imports). True if installed."""
    global _installed
    if transport is None:
        try:
            import js  # noqa: F401
        except ImportError:
            return False
        transport = BrowserTransport()
    import fsspec

    for scheme, cls in FILESYSTEMS.items():
        fsspec.register_implementation(scheme, cls, clobber=True)
    for cls in set(FILESYSTEMS.values()):
        cls.clear_instance_cache()
    _installed = transport
    return True


def uninstall() -> None:
    """Undo `install` (tests): fsspec finds its own filesystems again."""
    global _installed
    import importlib

    # The module: `from fsspec import registry` is its read-only view.
    known = importlib.import_module("fsspec.registry")._registry
    for scheme, cls in FILESYSTEMS.items():
        if known.get(scheme) is cls:
            known.pop(scheme, None)
    for cls in set(FILESYSTEMS.values()):
        cls.clear_instance_cache()
    _installed = None


def installed() -> bool:
    return _installed is not None


def handles(scheme) -> bool:
    """Whether `scheme` (remote_store's spelling: s3, gs, http, https) is read
    here."""
    return _installed is not None and str(scheme or "").lower() in CANONICAL
