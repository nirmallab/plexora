"""The OLE2 / Compound File Binary parser (`formats/cfb.py`) on its own, read
from compound files written to Microsoft's [MS-CFB] by
`formats_fixtures_more.write_cfb` (checked there against oiffile's
independent parser, not against this one).

Every stream's bytes are distinct (`stream_bytes`), so a sector taken from the
wrong place in a chain -- a FAT, mini FAT or DIFAT entry misread, a mini
sector taken as a regular one -- cannot pass. Chains are shuffled (no two
consecutive sectors of a stream adjacent in the file), in runs of three, or
contiguous; files are version 3 (512-byte sectors) and 4 (4096).
"""

import random
import threading

import pytest

from plexora.server.utils.formats import cfb
from tests.formats_fixtures_more import (HEADER_DIFAT, MINI_CUTOFF, CfbLayout, stream_bytes,
                                         write_cfb)

#: sizes either side of a mini sector, of a regular sector and of the cutoff
SIZES = {"Tiny": 1, "Mini63": 63, "Mini64": 64, "Mini65": 65, "Odd": 777,
         "Below": MINI_CUTOFF - 1, "At": MINI_CUTOFF, "Above": MINI_CUTOFF + 1,
         "Big": 70_000}


def _streams():
    streams = {}
    for number, (name, size) in enumerate(SIZES.items()):
        folder = "Image/Item(0)" if number % 3 == 0 else "Image" if number % 3 == 1 else ""
        path = f"{folder}/{name}" if folder else name
        streams[path] = stream_bytes(size, number + 1)
    # more small streams, so the mini stream spans several regular sectors
    for number in range(12):
        streams[f"Image/Item({number + 1})/Contents"] = stream_bytes(300 + 37 * number, 50 + number)
    return streams


class _Opened:
    def __init__(self, path, wrap=None):
        self.handle = open(path, "rb")
        self.cf = cfb.CompoundFile(wrap(self.handle) if wrap else self.handle)

    def __enter__(self):
        return self.cf

    def __exit__(self, *exc):
        self.cf.close()
        self.handle.close()


def _read(cf, path, offset=0, size=None):
    entry = cf.find(path)
    assert entry is not None, path
    return cf.read(entry, offset, size)


# -- every stream, every layout ------------------------------------------------


@pytest.mark.parametrize("tree", ["balanced", "chain", "left-chain"])
@pytest.mark.parametrize("layout", ["shuffled", "runs", "contiguous"])
@pytest.mark.parametrize("version", [3, 4])
def test_every_stream_reads_back_whole(tmp_path, version, layout, tree):
    streams = _streams()
    layout_info = write_cfb(tmp_path / "f.cfb", streams, version=version, layout=layout,
                            tree=tree, seed=11)
    # the fixture really put the small ones in the mini stream and the rest not
    for path, data in streams.items():
        assert layout_info.mini[path] == (len(data) < MINI_CUTOFF), path
    with _Opened(tmp_path / "f.cfb") as cf:
        for path, data in streams.items():
            entry = cf.find(path)
            assert entry is not None, path
            assert entry.is_stream
            assert entry.size == len(data), path
            got = cf.read(entry)
            assert len(got) == len(data), path
            assert got == data, f"{path} ({len(data)} bytes) read wrong"


def test_the_shuffled_fixture_chains_are_really_not_contiguous(tmp_path):
    """Guard on the fixture: the tests below lean on chains that jump."""
    layout_info = write_cfb(tmp_path / "f.cfb", _streams(), layout="shuffled", seed=11)
    big = layout_info.sectors["Big"]
    assert len(big) > 100
    assert sum(1 for a, b in zip(big, big[1:]) if b != a + 1) > len(big) // 2
    assert sorted(big) != big


@pytest.mark.parametrize("version", [3, 4])
def test_any_range_of_a_stream_equals_the_slice(tmp_path, version):
    streams = _streams()
    write_cfb(tmp_path / "f.cfb", streams, version=version, layout="shuffled", seed=3)
    rng = random.Random(5)
    sector = 512 if version == 3 else 4096
    with _Opened(tmp_path / "f.cfb") as cf:
        for path, data in streams.items():
            n = len(data)
            cases = [(0, n), (0, None), (n - 1, 1), (n // 2, None)]
            for boundary in (64, 128, sector, 2 * sector):
                if boundary < n:
                    cases += [(boundary - 1, 2), (boundary, 1), (boundary - 3, min(10, n - boundary + 3))]
            for _ in range(12):
                offset = rng.randrange(n)
                cases.append((offset, rng.randint(1, n - offset)))
            for offset, size in cases:
                want = data[offset:] if size is None else data[offset:offset + size]
                got = _read(cf, path, offset, size)
                assert got == want, f"{path}[{offset}:+{size}]"


# -- the directory ---------------------------------------------------------------


def test_the_directory_walk_finds_nested_entries_without_case(tmp_path):
    streams = _streams()
    write_cfb(tmp_path / "f.cfb", streams, storages=("Image/Empty", "Layers"), seed=2)
    with _Opened(tmp_path / "f.cfb") as cf:
        root = cf.find("")
        assert root is not None and root.kind == cfb.ROOT
        top = cf.children(root)
        assert set(top) == {"IMAGE", "LAYERS", "MINI64", "BELOW", "BIG"}
        image = cf.find("Image")
        assert image.kind == cfb.STORAGE and not image.is_stream
        names = set(cf.children(image))
        assert names == {f"ITEM({k})" for k in range(13)} | {"EMPTY", "MINI63", "ODD", "ABOVE"}
        assert cf.children(cf.find("Image/Empty")) == {}
        assert cf.children(cf.find("Layers")) == {}
        # names compare without case, at every level of the path
        assert _read(cf, "image/item(0)/TINY") == streams["Image/Item(0)/Tiny"]
        assert _read(cf, "IMAGE/ITEM(5)/contents") == streams["Image/Item(5)/Contents"]
        # an entry keeps its own spelling
        assert cf.find("image/item(0)/tiny").name == "Tiny"
        # what is not there is None, not an exception
        assert cf.find("Image/Nothing") is None
        assert cf.find("Nowhere/Tiny") is None
        assert cf.find("Big/Below") is None           # a stream has no children


@pytest.mark.parametrize("tree", ["balanced", "chain", "left-chain"])
def test_a_storage_with_many_children_lists_every_one(tmp_path, tree):
    """Each storage's children are a tree of siblings: a walk that follows only
    one side, or stops early, loses entries."""
    streams = {f"S/n{k:02d}{'x' * (k % 5)}": stream_bytes(10 + k, k) for k in range(40)}
    write_cfb(tmp_path / "f.cfb", streams, tree=tree, seed=4)
    with _Opened(tmp_path / "f.cfb") as cf:
        children = cf.children(cf.find("S"))
        assert set(children) == {path.split("/")[1].upper() for path in streams}
        for path, data in streams.items():
            assert cf.read(children[path.split("/")[1].upper()]) == data


@pytest.mark.parametrize("version", [3, 4])
def test_a_directory_of_many_sectors_in_a_shuffled_chain(tmp_path, version):
    # 300 entries: 75 directory sectors in version 3, 10 in version 4
    streams = {f"D{k // 30}/e{k}": stream_bytes(5 + k % 50, k) for k in range(290)}
    layout_info = write_cfb(tmp_path / "f.cfb", streams, version=version, seed=9)
    assert len(layout_info.directory_sectors) > 1
    with _Opened(tmp_path / "f.cfb") as cf:
        for path, data in streams.items():
            assert _read(cf, path) == data, path


def test_version_3_ignores_the_high_half_of_a_stream_size(tmp_path):
    streams = {"Small": stream_bytes(300, 1), "Large": stream_bytes(9000, 2)}
    write_cfb(tmp_path / "f.cfb", streams, version=3, size_high=0xDEADBEEF)
    with _Opened(tmp_path / "f.cfb") as cf:
        for path, data in streams.items():
            entry = cf.find(path)
            assert entry.size == len(data)
            assert cf.read(entry) == data


# -- a large file: the DIFAT past the header -----------------------------------


@pytest.fixture(scope="module")
def large_file(tmp_path_factory):
    """~7 MB of version-3 file: a stream placed past sector 109 * 128 needs a
    FAT sector the header does not list, found through a DIFAT sector."""
    folder = tmp_path_factory.mktemp("cfb_large")
    streams = {"Far": stream_bytes(40_000, 7), "Near": stream_bytes(300, 8),
               "Sub/Far2": stream_bytes(5_000, 9)}
    info = write_cfb(folder / "large.cfb", streams, version=3, layout="shuffled", gap=14_100)
    return folder / "large.cfb", streams, info


def test_streams_whose_fat_entries_are_listed_by_a_difat_sector(large_file):
    path, streams, info = large_file
    per = 512 // 4
    assert len(info.fat_sectors) > HEADER_DIFAT and info.difat_sectors
    # the far streams' chains run through FAT sectors only the DIFAT sector lists
    assert min(info.sectors["Far"]) // per >= HEADER_DIFAT
    with _Opened(path) as cf:
        for name, data in streams.items():
            assert _read(cf, name) == data, name
        assert _read(cf, "Far", 33_333, 5_000) == streams["Far"][33_333:38_333]


class _Counting:
    """A handle that counts what is read through it."""

    def __init__(self, handle):
        self._handle = handle
        self.bytes = 0
        self.calls = 0

    def seek(self, *args):
        return self._handle.seek(*args)

    def tell(self):
        return self._handle.tell()

    def read(self, size=-1):
        data = self._handle.read(size)
        self.bytes += len(data)
        self.calls += 1
        return data

    def __getattr__(self, name):
        return getattr(self._handle, name)


def test_nothing_is_read_ahead(large_file):
    """Opening reads the header, the DIFAT and the directory, not the 7 MB
    file nor its FAT; a small read reads a sector or so."""
    path, streams, _info = large_file
    counted = []
    with _Opened(path, wrap=lambda handle: counted.append(_Counting(handle)) or counted[-1]) as cf:
        counter = counted[0]
        opened = counter.bytes
        assert opened <= 16 * 512, f"opening read {opened} bytes"
        before = counter.bytes
        assert _read(cf, "Far", 20_000, 16) == streams["Far"][20_000:20_016]
        assert counter.bytes - before <= 48 * 512, f"16 bytes cost {counter.bytes - before}"


def test_a_contiguous_chain_is_read_in_runs(tmp_path):
    """137 contiguous sectors of one stream: a read per run, not per sector."""
    data = stream_bytes(70_000, 3)
    write_cfb(tmp_path / "f.cfb", {"Big": data}, layout="contiguous")
    counted = []
    with _Opened(tmp_path / "f.cfb",
                 wrap=lambda handle: counted.append(_Counting(handle)) or counted[-1]) as cf:
        entry = cf.find("Big")
        before = counted[0].calls
        assert cf.read(entry) == data
        assert counted[0].calls - before <= 8


# -- damage ------------------------------------------------------------------------


def test_a_cut_file_reads_zeros_where_it_holds_nothing(tmp_path):
    streams = {"Small": stream_bytes(2000, 1), "Big": stream_bytes(60_000, 2),
               "Other": stream_bytes(30_000, 3)}
    info: CfbLayout = write_cfb(tmp_path / "f.cfb", streams, version=3, layout="shuffled", seed=8)
    cut = info.file_size - 40 * 512 - 100
    data = (tmp_path / "f.cfb").read_bytes()[:cut]
    (tmp_path / "cut.cfb").write_bytes(data)
    with _Opened(tmp_path / "cut.cfb") as cf:
        for path, written in streams.items():
            got = _read(cf, path)
            want = info.held_after_cut(path, written, cut)
            assert len(got) == len(written), path
            assert got == want, path
        # some of Big really was lost and some kept
        assert _read(cf, "Big") != streams["Big"]
        # a range of only lost bytes
        lost = [i for i, (o, n) in enumerate(info.pieces["Big"]) if o >= cut]
        if lost:
            start = sum(n for _o, n in info.pieces["Big"][:lost[0]])
            assert _read(cf, "Big", start, 100) == bytes(100)


@pytest.mark.parametrize("content", [b"", b"\x00" * 600, b"not a compound file at all" * 40,
                                     b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"])
def test_what_is_not_a_compound_file_is_refused(tmp_path, content):
    path = tmp_path / "x.bin"
    path.write_bytes(content)
    with open(path, "rb") as handle:
        with pytest.raises(cfb.CompoundFileError):
            cfb.CompoundFile(handle)


def test_a_header_whose_sector_size_is_not_3_or_4_is_refused(tmp_path):
    write_cfb(tmp_path / "f.cfb", {"A": stream_bytes(100, 1)})
    data = bytearray((tmp_path / "f.cfb").read_bytes())
    data[30:32] = (20).to_bytes(2, "little")        # 1 MiB sectors
    (tmp_path / "bad.cfb").write_bytes(bytes(data))
    with open(tmp_path / "bad.cfb", "rb") as handle:
        with pytest.raises(cfb.CompoundFileError):
            cfb.CompoundFile(handle)


def test_a_file_cut_after_its_header_is_refused_not_crashed(tmp_path):
    write_cfb(tmp_path / "f.cfb", _streams(), seed=1)
    (tmp_path / "cut.cfb").write_bytes((tmp_path / "f.cfb").read_bytes()[:512])
    with open(tmp_path / "cut.cfb", "rb") as handle:
        with pytest.raises(cfb.CompoundFileError):
            cfb.CompoundFile(handle)


def test_a_chain_that_loops_does_not_hang(tmp_path):
    """A FAT entry pointing back into its own chain (a damaged file) must end
    the read -- as zeros or an error -- not loop for ever."""
    data = stream_bytes(20_000, 1)
    info = write_cfb(tmp_path / "f.cfb", {"Big": data}, layout="contiguous")
    raw = bytearray((tmp_path / "f.cfb").read_bytes())
    chain = info.sectors["Big"]
    fat_at = (info.fat_sectors[0] + 1) * 512
    raw[fat_at + 4 * chain[10]:fat_at + 4 * chain[10] + 4] = chain[2].to_bytes(4, "little")
    (tmp_path / "loop.cfb").write_bytes(bytes(raw))
    result = {}

    def run():
        try:
            with _Opened(tmp_path / "loop.cfb") as cf:
                result["data"] = _read(cf, "Big")
        except cfb.CompoundFileError as exc:
            result["error"] = exc
        except Exception as exc:  # noqa: BLE001
            result["crash"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "reading a looping chain did not return"
    assert "crash" not in result, result.get("crash")
    if "data" in result:
        assert len(result["data"]) == len(data)
        assert result["data"][:11 * 512] == data[:11 * 512]


# -- threads -----------------------------------------------------------------------


def test_one_handle_serves_many_threads(tmp_path):
    streams = _streams()
    write_cfb(tmp_path / "f.cfb", streams, version=4, layout="shuffled", seed=6)
    paths = list(streams)
    failures = []
    with _Opened(tmp_path / "f.cfb") as cf:
        def work(seed):
            rng = random.Random(seed)
            for _ in range(60):
                path = rng.choice(paths)
                data = streams[path]
                offset = rng.randrange(len(data))
                size = rng.randint(1, len(data) - offset)
                if cf.read(cf.find(path), offset, size) != data[offset:offset + size]:
                    failures.append((path, offset, size))

        threads = [threading.Thread(target=work, args=(seed,)) for seed in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert not failures, failures[:5]
