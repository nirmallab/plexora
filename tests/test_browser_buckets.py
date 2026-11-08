"""Buckets on the website: `s3://`, `gs://` and `https://` read through the
page's `fetch` (plexora/browser/buckets.py), where aiohttp, s3fs and gcsfs
cannot run.

Before, opening `s3://...` on the website said "Reading s3:// addresses needs
the s3fs package, which is not installed". Here a stand-in for the page's
fetch (`Cloud`) answers as the real services answer a web page, which is
narrower than what they tell a desktop client:

- S3 (a public bucket with CORS, as `lin-2023-orion-crc`): GET only -- HEAD
  fails as a refused cross-site request does -- and no `Content-Range` or
  `ETag` header a page may read, so a size comes from ListObjectsV2.
- Google Cloud Storage: the JSON API (`storage/v1`) for listing, records and
  bytes (`alt=media`); its XML host is not used, since it refuses pages for
  buckets with no CORS settings (IDC's `idc-open-data`).
- A web host: HEAD where its CORS admits it, an index page to list a folder.

Measured on the live preview with this module injected (2026-11-08):
Orion CRC02 (73 GB, `s3://lin-2023-orion-crc`) and IDC's HTA7_920_1000 DICOM
slide (`gs://idc-open-data/fc078201-...`, 216 instances) both open and draw,
with JSPI and without (the synchronous path, as in Firefox and Safari).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import threading
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

import numpy as np
import pytest

from plexora.browser import boot, buckets
from plexora.server.utils import remote_store
from tests.remote_fixtures import cache_root  # noqa: F401


def run(coroutine):
    return asyncio.run(coroutine)


def _span(header, size):
    """(start, end) for a Range header, as a server reads it; None if absent."""
    if not header:
        return None
    spec = header.split("=", 1)[1]
    first, _, last = spec.partition("-")
    if first == "":
        return max(0, size - int(last)), size
    start = int(first)
    return start, (min(size, int(last) + 1) if last else size)


class Cloud(buckets.Transport):
    """The page's `fetch`, against S3, Google's JSON API and one web host."""

    def __init__(self):
        self.s3: dict[str, dict[str, bytes]] = {}
        self.gcs: dict[str, dict[str, bytes]] = {}
        self.web: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, dict, bool]] = []
        self.page_size = 1000
        self.s3_lists = True       # the bucket lets anyone list it
        self.cors = True           # every host lets this page read it
        self.web_head = True       # the web host's CORS admits HEAD
        self.reachable = True      # a no-cors request gets an answer
        self.early = True          # fetch (headers first), not a sync XHR

    def stops_early(self):
        return self.early

    async def reaches(self, url):
        return self.reachable

    async def request(self, method, url, headers=None, *, body=True, timeout=90):
        headers = dict(headers or {})
        self.requests.append((method, url, headers, body))
        if not self.cors:
            raise buckets.PageUnreachable("Failed to fetch")
        parts = urlsplit(url)
        if parts.netloc.endswith("amazonaws.com"):
            answer = self._s3(method, parts, headers)
        elif parts.netloc == "storage.googleapis.com":
            answer = self._gcs(method, parts, headers)
        else:
            answer = self._host(method, url, headers)
        if not body:
            answer.body = b""
        return answer

    @staticmethod
    def _bytes(data, headers, *, content_range):
        span = _span(headers.get("Range"), len(data))
        if span is None:
            return buckets.Answer(200, {"content-length": str(len(data))}, data)
        start, end = span
        if start >= len(data):
            return buckets.Answer(416, {})
        exposed = {"content-length": str(end - start)}
        if content_range:
            exposed["content-range"] = f"bytes {start}-{end - 1}/{len(data)}"
        return buckets.Answer(206, exposed, data[start:end])

    @staticmethod
    def _listing(keys, prefix, delimiter):
        """Sorted [(kind, name)] directly under `prefix`, as both services list."""
        rows = set()
        for key in keys:
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if delimiter and delimiter in rest:
                rows.add(("prefix", prefix + rest.split(delimiter)[0] + delimiter))
            else:
                rows.add(("key", key))
        return sorted(rows, key=lambda row: row[1])

    def _s3(self, method, parts, headers):
        if method != "GET":
            raise buckets.PageUnreachable("Failed to fetch")  # CORS admits GET only
        if parts.netloc.startswith("s3."):  # path-style
            bucket, _, key = parts.path.lstrip("/").partition("/")
        else:
            bucket, key = parts.netloc.split(".s3.")[0], parts.path.lstrip("/")
        objects = self.s3.get(bucket)
        if objects is None:
            return buckets.Answer(404, {})
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        if not key and query.get("list-type") == "2":
            if not self.s3_lists:
                return buckets.Answer(403, {"content-type": "application/xml"}, b"<Error/>")
            rows = self._listing(objects, query.get("prefix", ""), query.get("delimiter"))
            start = int(query.get("continuation-token") or 0)
            count = min(int(query.get("max-keys", 1000)), self.page_size)
            page, more = rows[start:start + count], start + count < len(rows)
            xml = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
                   "<ListBucketResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">",
                   f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>"]
            if more:
                xml.append(f"<NextContinuationToken>{start + count}</NextContinuationToken>")
            for kind, name in page:
                if kind == "key":
                    xml.append(f"<Contents><Key>{escape(name)}</Key><Size>{len(objects[name])}</Size>"
                               f"<ETag>&quot;etag-{len(objects[name])}&quot;</ETag>"
                               "<LastModified>2023-07-12T20:42:31.000Z</LastModified></Contents>")
                else:
                    xml.append(f"<CommonPrefixes><Prefix>{escape(name)}</Prefix></CommonPrefixes>")
            xml.append("</ListBucketResult>")
            return buckets.Answer(200, {"content-type": "application/xml"}, "".join(xml).encode())
        key = unquote(key)
        if key not in objects:
            return buckets.Answer(404 if self.s3_lists else 403, {})
        answer = self._bytes(objects[key], headers, content_range=False)
        answer.headers["last-modified"] = "Wed, 12 Jul 2023 20:42:31 GMT"
        return answer

    def _gcs(self, method, parts, headers):
        segments = parts.path.split("/")
        # /storage/v1/b/<bucket>/o[/<object>]: never the XML host.
        assert segments[1:3] == ["storage", "v1"] and segments[3] == "b", parts.path
        bucket = unquote(segments[4])
        objects = self.gcs.get(bucket)
        if objects is None:
            return buckets.Answer(404, {})
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}

        def record(name):
            return {"name": name, "size": str(len(objects[name])), "etag": f"E{len(objects[name])}",
                    "generation": "1729819015790308", "updated": "2024-10-25T01:16:55.891Z"}

        if len(segments) == 6:  # objects.list
            rows = self._listing(objects, query.get("prefix", ""), query.get("delimiter"))
            start = int(query.get("pageToken") or 0)
            count = min(int(query.get("maxResults", 1000)), self.page_size)
            page = rows[start:start + count]
            found = {"items": [record(n) for k, n in page if k == "key"],
                     "prefixes": [n for k, n in page if k == "prefix"]}
            if start + count < len(rows):
                found["nextPageToken"] = str(start + count)
            return buckets.Answer(200, {"content-type": "application/json"}, json.dumps(found).encode())
        name = unquote(segments[6])
        if name not in objects:
            return buckets.Answer(404, {})
        if query.get("alt") == "media":
            return self._bytes(objects[name], headers, content_range=True)
        return buckets.Answer(200, {"content-type": "application/json"}, json.dumps(record(name)).encode())

    def _host(self, method, url, headers):
        if method == "HEAD" and not self.web_head:
            raise buckets.PageUnreachable("Failed to fetch")
        if url not in self.web:
            return buckets.Answer(404, {})
        data = self.web[url]
        answer = self._bytes(data, headers, content_range=True)
        answer.headers["etag"] = '"w1"'
        return answer


@pytest.fixture
def cloud(cache_root):  # noqa: F811
    stand_in = Cloud()
    buckets.install(stand_in)
    yield stand_in
    buckets.uninstall()


def _fs(scheme, **options):
    import fsspec

    return fsspec.filesystem(scheme, asynchronous=True, **options)


BLOB = bytes((i * 7 + i // 251) % 256 for i in range(200_000))


# -- s3:// ---------------------------------------------------------------------


def test_s3_is_sized_by_its_listing_and_read_in_ranges(cloud):
    cloud.s3["lab"] = {"run/slide.ome.tiff": BLOB, "run/cells.csv": b"a,b\n", "run/seg/mask.tif": b"m"}
    fs = _fs("s3")

    info = run(fs._info("s3://lab/run/slide.ome.tiff"))
    assert (info["type"], info["size"], info["ETag"]) == ("file", len(BLOB), '"etag-200000"')
    assert run(fs._info("lab/run/seg"))["type"] == "directory"
    with pytest.raises(FileNotFoundError):
        run(fs._info("lab/run/none.tif"))
    # Never a HEAD: a public bucket's CORS admits GET only.
    assert {method for method, *_ in cloud.requests} == {"GET"}

    assert run(fs._cat_file("lab/run/slide.ome.tiff", start=100, end=1100)) == BLOB[100:1100]
    assert run(fs._cat_file("lab/run/slide.ome.tiff", start=-16)) == BLOB[-16:]
    assert run(fs._cat_file("lab/run/slide.ome.tiff", start=199_990)) == BLOB[199_990:]
    assert cloud.requests[-1][2] == {"Range": "bytes=199990-"}
    assert run(fs._cat_file("lab/run/slide.ome.tiff", start=10, end=-10)) == BLOB[10:-10]
    with pytest.raises(OSError, match="not satisfiable"):
        run(fs._cat_file("lab/run/slide.ome.tiff", start=300_000, end=300_010))

    listed = {entry["name"]: entry["type"] for entry in run(fs._ls("s3://lab/run/"))}
    assert listed == {"lab/run/slide.ome.tiff": "file", "lab/run/cells.csv": "file",
                      "lab/run/seg": "directory"}
    assert cloud.requests[-1][1] == ("https://lab.s3.amazonaws.com/?list-type=2&prefix=run%2F"
                                     "&delimiter=%2F&max-keys=1000")


def test_s3_listings_are_followed_page_by_page(cloud):
    cloud.s3["lab"] = {f"plate/{i:02d}.tif": b"x" * i for i in range(7)}
    cloud.page_size = 3
    names = run(_fs("s3")._ls("lab/plate", detail=False))
    assert names == [f"lab/plate/{i:02d}.tif" for i in range(7)]
    assert sum("list-type=2" in url for _, url, *_ in cloud.requests) == 3


def test_a_bucket_that_will_not_list_is_sized_by_the_object_itself(cloud):
    cloud.s3["closed"] = {"slide.tif": BLOB}
    cloud.s3_lists = False
    info = run(_fs("s3")._info("closed/slide.tif"))
    assert (info["size"], info["LastModified"]) == (len(BLOB), "Wed, 12 Jul 2023 20:42:31 GMT")
    method, url, headers, body = cloud.requests[-1]
    assert (url, headers, body) == ("https://closed.s3.amazonaws.com/slide.tif", {}, False)
    # A key that is not there is "forbidden" in such a bucket, as on the desktop.
    with pytest.raises(PermissionError):
        run(_fs("s3")._info("closed/other.tif"))

    # Without JSPI a request is a synchronous XHR, which reads a body whole:
    # one byte is asked for, never the object.
    cloud.early = False
    buckets.S3PageFileSystem.clear_instance_cache()
    run(_fs("s3")._info("closed/slide.tif"))
    assert cloud.requests[-1][2] == {"Range": "bytes=0-0"}


def test_s3_addresses_follow_the_address_book(cloud):
    fs = _fs("s3", region="eu-west-2")
    assert fs._url("lab", "a b/c.tif") == "https://lab.s3.eu-west-2.amazonaws.com/a%20b/c.tif"
    fs = _fs("s3", client_kwargs={"endpoint_url": "https://minio.example.org/"})
    assert fs._url("lab", "c.tif") == "https://minio.example.org/lab/c.tif"
    assert _fs("s3")._url("my.lab", "c.tif") == "https://s3.amazonaws.com/my.lab/c.tif"


# -- gs:// ---------------------------------------------------------------------


def test_gcs_goes_through_the_json_api(cloud):
    cloud.gcs["idc-open-data"] = {"series/a.dcm": BLOB, "series/b.dcm": b"b" * 10, "series/deep/c.dcm": b"c"}
    fs = _fs("gs")

    info = run(fs._info("gs://idc-open-data/series/a.dcm"))
    assert (info["size"], info["etag"], info["generation"]) == (len(BLOB), "E200000", "1729819015790308")
    assert run(fs._info("idc-open-data/series/deep"))["type"] == "directory"
    with pytest.raises(FileNotFoundError):
        run(fs._info("idc-open-data/series/zz.dcm"))

    assert run(fs._cat_file("idc-open-data/series/a.dcm", start=65_536, end=131_072)) == BLOB[65_536:131_072]
    method, url, headers, _ = cloud.requests[-1]
    assert url == ("https://storage.googleapis.com/storage/v1/b/idc-open-data/o/"
                   "series%2Fa.dcm?alt=media")
    assert headers == {"Range": "bytes=65536-131071"}

    cloud.page_size = 2
    listed = {entry["name"]: entry["type"] for entry in run(fs._ls("idc-open-data/series"))}
    assert listed == {"idc-open-data/series/a.dcm": "file", "idc-open-data/series/b.dcm": "file",
                      "idc-open-data/series/deep": "directory"}
    assert all(url.startswith("https://storage.googleapis.com/storage/v1/") for _, url, *_ in cloud.requests)


# -- https:// ------------------------------------------------------------------


def test_a_web_host_is_sized_by_head_or_by_a_get(cloud):
    url = "https://data.example.org/images/slide.ome.tiff"
    cloud.web[url] = BLOB
    fs = _fs("https")
    assert run(fs._info(url))["size"] == len(BLOB)
    assert cloud.requests[-1][0] == "HEAD"
    cloud.web_head = False
    info = run(fs._info(url + "/"))
    assert (info["name"], info["size"]) == (url, len(BLOB))
    assert cloud.requests[-1][:1] == ("GET",) and cloud.requests[-1][3] is False
    assert run(fs._cat_file(url, start=5, end=9)) == BLOB[5:9]


def test_a_web_folder_is_listed_from_its_index_page(cloud):
    base = "https://data.example.org/plate"
    cloud.web[base + "/"] = (b'<a href="A1.ome.zarr/">A1</a> <a href="../">up</a> '
                             b'<a href="notes.txt">n</a> <A HREF="https://elsewhere.org/x">x</A>')
    listed = run(_fs("https")._ls(base))
    assert {e["name"]: e["type"] for e in listed} == {f"{base}/A1.ome.zarr": "directory",
                                                      f"{base}/notes.txt": "file"}


# -- a host that does not let pages read it ------------------------------------


def test_a_host_without_cors_says_so(cloud):
    cloud.s3["private-lab"] = {"slide.tif": BLOB}
    cloud.cors = False
    with pytest.raises(buckets.PageRefused, match="does not let web pages read it"):
        run(_fs("s3")._cat_file("private-lab/slide.tif", start=0, end=10))
    # Not even a no-cors answer: the network, not the host's settings.
    cloud.reachable = False
    with pytest.raises(buckets.PageUnreachable):
        run(_fs("s3")._cat_file("private-lab/slide.tif", start=0, end=10))


# -- remote_store on top -------------------------------------------------------


def test_remote_store_needs_none_of_the_desktop_packages_on_the_page(cloud, monkeypatch):
    for module in ("aiohttp", "s3fs", "gcsfs"):
        monkeypatch.setitem(sys.modules, module, None)
    support = remote_store.support()
    assert (support["https"], support["s3"], support["gs"]) == (True, True, True)
    for url in ("s3://lab/a.zarr", "gs://lab/a.zarr", "https://h.example.org/a.zarr"):
        remote_store._require_scheme(url)
        remote_store.storage_options_for(url)  # aiohttp is not imported
    assert remote_store.storage_options_for(
        "s3://lab/a.zarr", {"anon": True, "region": "eu-west-2", "profile": "lab"}) == {
        "anon": True, "region": "eu-west-2"}

    buckets.uninstall()
    with pytest.raises(remote_store.RemoteSupportMissing, match="s3fs"):
        remote_store._require_scheme("s3://lab/a.zarr")
    assert remote_store.support()["gs"] is False


def test_an_ome_zarr_in_s3_is_read_through_the_page_and_cached(cloud):
    import zarr

    data = np.arange(64 * 80, dtype=np.uint16).reshape(64, 80)
    memory = zarr.storage.MemoryStore()
    array = zarr.create_array(memory, name="0", shape=data.shape, chunks=(32, 32), dtype=data.dtype)
    array[:] = data
    cloud.s3["lab"] = {f"img.zarr/{key}": value.to_bytes() for key, value in memory._store_dict.items()}

    store, subpath = remote_store.open_store("s3://lab/img.zarr/0")
    assert subpath == "0"
    read = zarr.open_array(store=store, path=subpath, mode="r")[:]
    assert np.array_equal(read, data)
    fetched = len(cloud.requests)
    assert np.array_equal(zarr.open_array(store=store, path=subpath, mode="r")[:], data)
    assert len(cloud.requests) == fetched  # the second read is the cache's


def test_a_tiff_in_gcs_is_read_through_the_page(cloud):
    import tifffile

    data = (np.arange(3 * 96 * 112) % 65521).astype(np.uint16).reshape(3, 96, 112)
    buffer = io.BytesIO()
    tifffile.imwrite(buffer, data, tile=(32, 32), photometric="minisblack")
    cloud.gcs["lab"] = {"run/slide.tif": buffer.getvalue()}

    with remote_store.open_file("gs://lab/run/slide.tif") as handle:
        with tifffile.TiffFile(handle) as tiff:
            assert np.array_equal(tiff.asarray(), data)


def test_a_host_without_cors_reaches_the_reader_as_its_own_sentence(cloud):
    cloud.s3["private-lab"] = {"slide.tif": BLOB}
    cloud.cors = False
    # Its size asked of the host, and its size already known (a listing said).
    for size in (None, len(BLOB)):
        with pytest.raises(PermissionError, match="does not let web pages read it") as caught:
            remote_store.open_file("s3://private-lab/slide.tif", size=size).read(16)
        assert "private-lab.s3.amazonaws.com" in str(caught.value)
    # A zarr store: one sentence, not "no zarr metadata here".
    import zarr

    store, subpath = remote_store.open_store("s3://private-lab/img.zarr")
    with pytest.raises(PermissionError, match="does not let web pages read it"):
        zarr.open_group(store=store, path=subpath, mode="r")


# -- the boot ------------------------------------------------------------------


def test_the_website_loop_runs_executor_calls_where_it_is(monkeypatch):
    """Without JSPI coroutines run on `boot.SyncLoop`, and the chunk cache
    reads its files through `asyncio.to_thread`: no thread may start."""
    def no_threads(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", no_threads)

    async def read():
        return await asyncio.to_thread(lambda a, b: a + b, 2, 5)

    assert boot.run_to_completion(read()) == 7

    async def fail():
        return await asyncio.to_thread(lambda: 1 / 0)

    with pytest.raises(ZeroDivisionError):
        boot.run_to_completion(fail())


def test_the_website_leaves_the_cache_budget_to_the_setting(monkeypatch):
    """The cache is kept in the site's storage (tests/test_browser_cachefiles.py),
    not in the worker's memory: the boot no longer caps it."""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.pop("PLEXORA_REMOTE_CACHE_BYTES", None)
    boot.environment("https://viewer.example.org")
    assert "PLEXORA_REMOTE_CACHE_BYTES" not in os.environ


def test_nothing_is_installed_outside_a_browser():
    assert buckets.install() is False
    assert not buckets.installed()


def test_a_dicom_folder_is_never_probed_for_zarr_under_each_instance(cloud):
    """A listed `.dcm` is a file: asking each one for zarr's five metadata
    names cost an IDC series of 216 instances 1,080 requests, all 404
    (ome_zarr._RemoteView.children)."""
    from plexora.server.utils import ome_zarr

    cloud.s3["lab"] = {f"series/{n:03d}.dcm": b"DICM" * 8 for n in range(40)}
    assert ome_zarr.image_candidates("s3://lab/series") == []
    under_instances = [url for _method, url, _headers, _body in cloud.requests if ".dcm/" in url]
    assert under_instances == []
    assert len(cloud.requests) < 40
