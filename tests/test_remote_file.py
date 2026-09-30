"""One file at a web address, read as a seekable stream through the chunk cache.

What DICOM, TIFF and picture streaming all stand on. The behaviour under test
is the part a reader never sees: which byte ranges a run of reads turns into,
that each lands in the cache once, and that a second open -- even in a fresh
process with the host gone -- costs nothing.
"""

import threading
import time

import pytest

from plexora.server.providers.base import RemoteUnreachable
from plexora.server.utils import remote_store
from plexora.server.utils.remote_store import (
    HEAD_BYTES, RemoteFile, RemoteListingUnavailable, RemoteReadTooLarge)
from tests.remote_fixtures import (cache_root, closed_port_url,  # noqa: F401
                                   http_store)

SIZE = 3 * 1024 * 1024 + 12345


def _blob(n=SIZE) -> bytes:
    return bytes((i * 7 + i // 251) % 256 for i in range(n))


@pytest.fixture
def served(tmp_path):
    root = tmp_path / "served"
    root.mkdir(exist_ok=True)
    (root / "blob.tif").write_bytes(_blob())
    return root


def _ranges(cache_root, name="blob.tif"):
    return sorted(p.name for p in cache_root.rglob(f"{name}.ranges/*"))


# -- URLs -------------------------------------------------------------------


def test_a_file_is_keyed_under_the_folder_it_sits_in():
    split = remote_store.split_locator_url
    assert split("https://h/a/b/slide.ome.tif") == ("https://h/a/b", "slide.ome.tif")
    assert split("gs://bucket/series/x.dcm") == ("gs://bucket/series", "x.dcm")
    assert split("https://h/a/DICOMDIR") == ("https://h/a", "DICOMDIR")
    assert split("gs://bucket/dicom-folder/") == ("gs://bucket/dicom-folder", "")


def test_zarr_addresses_split_exactly_as_before():
    for url in ("https://h/x.zarr", "https://h/x.zarr/0", "s3://b/p/store.zarr/images/a",
                "https://h/plate.ome.zarr/A/1/0", "https://h/unsuffixed-store",
                "https://h/x.zarr/labels/cells.tif"):
        assert remote_store.split_locator_url(url) == remote_store.split_store_url(url)


def test_is_file_url_is_a_name_rule():
    assert remote_store.is_file_url("https://h/a/slide.SVS")
    assert remote_store.is_file_url("https://h/pic.jpeg")
    assert not remote_store.is_file_url("https://h/a/folder")
    assert not remote_store.is_file_url("https://h/x.zarr/0/c/0.tif")
    assert not remote_store.is_file_url("https://h/table.csv")


# -- reads ------------------------------------------------------------------


def test_small_reads_share_one_aligned_block(served, http_store, cache_root):
    server = http_store()
    handle = remote_store.open_file(server.url("blob.tif"))
    expected = _blob()

    handle.seek(10)
    assert handle.read(8) == expected[10:18]
    handle.seek(40_000)
    assert handle.read(100) == expected[40_000:40_100]

    assert server.count("blob.tif", method="GET") == 1
    assert _ranges(cache_root) == [f"0_{64 * 1024}"]


def test_a_read_across_two_blocks_fetches_them_together(served, http_store):
    server = http_store()
    handle = remote_store.open_file(server.url("blob.tif"))
    server.delay = 0.3
    started = time.monotonic()
    data = handle.pread(64 * 1024 - 10, 20)
    elapsed = time.monotonic() - started
    assert data == _blob()[64 * 1024 - 10:64 * 1024 + 10]
    assert server.count("blob.tif", method="GET") == 2
    assert elapsed < 0.55, elapsed


def test_a_large_read_uses_the_large_grid(served, http_store, cache_root):
    server = http_store()
    handle = remote_store.open_file(server.url("blob.tif"))
    assert handle.pread(0, 300 * 1024) == _blob()[:300 * 1024]
    assert _ranges(cache_root) == [f"0_{2 * 1024 * 1024}"]


def test_the_last_block_is_short_and_says_how_long_the_file_is(served, http_store):
    server = http_store()
    handle = remote_store.open_file(server.url("blob.tif"))
    tail = handle.pread(SIZE - 100, 4096)
    assert tail == _blob()[-100:]
    assert handle.known_size == SIZE
    assert server.count("blob.tif", method="HEAD") == 0
    assert handle.pread(SIZE + 10, 5) == b""


def test_the_length_is_asked_for_once(served, http_store):
    server = http_store()
    handle = remote_store.open_file(server.url("blob.tif"))
    assert handle.seek(0, 2) == SIZE
    assert handle.seek(0, 2) == SIZE
    assert server.count("blob.tif", method="HEAD") <= 1
    assert server.count("blob.tif", method="GET") == 0


def test_a_second_process_reads_everything_from_disk(served, http_store, cache_root):
    server = http_store()
    url = server.url("blob.tif")
    first = remote_store.open_file(url)
    assert first.pread(5000, 3000) == _blob()[5000:8000]
    assert first.seek(0, 2) == SIZE
    remote_store.cache_index().flush()

    remote_store._reset_for_tests(cache_root)
    server.clear()
    with server.outage():
        again = remote_store.open_file(url)
        assert again.pread(5000, 3000) == _blob()[5000:8000]
        # The length survived too, so seeking to the end needs no host.
        assert again.seek(0, 2) == SIZE
    assert server.count(method="GET") == 0


def test_read_everything_is_capped(served, http_store, monkeypatch):
    server = http_store()
    monkeypatch.setattr(remote_store, "_READ_ALL_MAX", 1024 * 1024)
    handle = remote_store.open_file(server.url("blob.tif"))
    with pytest.raises(RemoteReadTooLarge):
        handle.read()
    handle.seek(SIZE - 1000)
    assert handle.read() == _blob()[-1000:]


def test_readinto_fills_the_buffer(served, http_store):
    server = http_store()
    handle = remote_store.open_file(server.url("blob.tif"))
    buffer = bytearray(1000)
    handle.seek(123)
    assert handle.readinto(buffer) == 1000
    assert bytes(buffer) == _blob()[123:1123]
    assert handle.tell() == 1123


# -- failures -----------------------------------------------------------------


def test_a_dead_host_is_unreachable_and_cached_blocks_still_read(served, http_store):
    server = http_store()
    handle = remote_store.open_file(server.url("blob.tif"))
    assert handle.pread(0, 10) == _blob()[:10]
    remote_store._block_memory.clear()
    with server.outage():
        assert handle.pread(0, 10) == _blob()[:10]
        with pytest.raises(RemoteUnreachable):
            handle.pread(SIZE - 10, 10)


def test_a_closed_port_is_unreachable(cache_root):
    handle = remote_store.open_file(closed_port_url("nothing/slide.tif"))
    with pytest.raises(RemoteUnreachable):
        handle.pread(0, 10)


def test_missing_and_forbidden_files(served, http_store):
    server = http_store()
    with pytest.raises(FileNotFoundError):
        remote_store.open_file(server.url("absent.tif")).pread(0, 10)
    forbidden = http_store("forbidden")
    with pytest.raises(PermissionError):
        remote_store.open_file(forbidden.url("absent.tif")).pread(0, 10)


def test_a_host_that_ignores_range_is_read_whole_when_small(tmp_path, http_store, cache_root):
    root = tmp_path / "norange"
    root.mkdir()
    (root / "small.png").write_bytes(_blob(200_000))
    server = http_store("norange", root=root)
    handle = remote_store.open_file(server.url("small.png"))
    assert handle.pread(1000, 10) == _blob(200_000)[1000:1010]
    assert handle.pread(150_000, 10) == _blob(200_000)[150_000:150_010]
    assert server.count("small.png", method="GET") == 1
    # The oversized answer was not kept under a range key.
    assert _ranges(cache_root, "small.png") == []


def test_a_host_that_ignores_range_is_refused_for_a_large_file(served, http_store,
                                                              monkeypatch):
    monkeypatch.setattr(remote_store, "_WHOLE_FILE_MAX", 1024 * 1024)
    server = http_store("norange")
    with pytest.raises(ValueError, match="byte ranges"):
        remote_store.open_file(server.url("blob.tif")).pread(0, 10)


def test_a_read_from_zarrs_loop_is_refused_rather_than_hanging(served, http_store):
    from zarr.core.sync import sync

    server = http_store()
    handle = remote_store.open_file(server.url("blob.tif"))

    async def on_the_loop():
        return handle.pread(0, 10)

    outcome = {}

    def run():
        try:
            sync(on_the_loop(), timeout=10)
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(15)
    assert not thread.is_alive()
    assert "IO loop" in str(outcome.get("error"))


# -- whole files and listings ----------------------------------------------


def test_read_bytes_is_one_entry(served, http_store, cache_root):
    server = http_store()
    (served / "pic.png").write_bytes(b"\x89PNG" + b"x" * 5000)
    url = server.url("pic.png")
    assert remote_store.read_bytes(url) == b"\x89PNG" + b"x" * 5000
    assert remote_store.read_bytes(url) == b"\x89PNG" + b"x" * 5000
    assert server.count("pic.png", method="GET") == 1
    # A range read afterwards is served from the whole value.
    assert remote_store.open_file(url).pread(0, 4) == b"\x89PNG"
    assert server.count("pic.png", method="GET") == 1


def test_list_files_walks_a_listing_host(served, http_store):
    (served / "slide" / "study" / "series").mkdir(parents=True)
    for name in ("a.dcm", "b.dcm", "notes.txt"):
        (served / "slide" / "study" / "series" / name).write_bytes(b"x" * 10)
    (served / "slide" / "top.dcm").write_bytes(b"y" * 20)
    server = http_store("listing")
    found = remote_store.list_files(server.url("slide"), suffixes=(".dcm",))
    assert found == [server.url("slide", "top.dcm"),
                     server.url("slide", "study", "series", "a.dcm"),
                     server.url("slide", "study", "series", "b.dcm")]


def test_list_files_on_a_gateway_says_it_cannot_list(served, http_store):
    (served / "slide").mkdir()
    (served / "slide" / "a.dcm").write_bytes(b"x")
    server = http_store()
    with pytest.raises(RemoteListingUnavailable):
        remote_store.list_files(server.url("slide"))


def test_a_member_url_lands_in_its_folders_store(served, http_store, cache_root):
    (served / "slide").mkdir()
    (served / "slide" / "a.dcm").write_bytes(_blob(1000))
    server = http_store("listing")
    folder, _ = remote_store.open_store(server.url("slide"))
    store, key = remote_store.file_store(server.url("slide", "a.dcm"))
    assert store is folder and key == "a.dcm"


def test_a_file_probe_reports_its_identity(served, http_store):
    server = http_store()
    result = remote_store.probe(server.url("blob.tif"), fresh=True)
    assert result.status == "ok" and result.etag and result.size == SIZE
    assert remote_store.probe(server.url("absent.tif"), fresh=True).status == "missing"


def test_head_bytes_is_two_small_blocks():
    assert HEAD_BYTES == 2 * 64 * 1024
    assert issubclass(RemoteFile, object)


def test_a_long_cache_root_still_caches(tmp_path, http_store, monkeypatch):
    """Past Windows' 260-character path limit, blocks are still kept.

    Object stores name files by UUID, and a data root inside a synced folder
    is long: together they passed the limit, and every block write failed
    without a word. The index now writes through the extended-length form.
    """
    import os

    deep = tmp_path / ("d" * 60) / ("e" * 60)
    root = deep / ".remote_cache"
    remote_store._reset_for_tests(root)
    try:
        name = "0" * 36 + "-long-instance-name-from-an-object-store.tif"
        served = tmp_path / "served"
        served.mkdir(exist_ok=True)
        (served / name).write_bytes(_blob(200_000))
        server = http_store()
        url = server.url(name)
        assert remote_store.open_file(url).pread(100, 50) == _blob(200_000)[100:150]
        written = remote_store.cache_index().value_path(
            remote_store.open_store(url)[0].store_id, name, (0, 64 * 1024))
        if os.name == "nt":
            assert len(str(written)) > 260
        assert written.exists()

        remote_store.cache_index().flush()
        remote_store._reset_for_tests(root)
        server.clear()
        assert remote_store.open_file(url).pread(100, 50) == _blob(200_000)[100:150]
        assert server.count(method="GET") == 0
    finally:
        remote_store._reset_for_tests()
