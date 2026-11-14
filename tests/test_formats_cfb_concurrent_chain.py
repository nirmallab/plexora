"""A compound file's stream chain grown by two threads at once.

`CompoundFile` caches each stream's sector chain on its `Entry` and grows it
only as far as a read reaches; following it reads FAT pages from the file.
Waitress serves a viewer's tiles on many threads, so the first reads of a
plane (a ZVI z-stack's other planes, every channel asked at once) can walk
the same chain together.

The interleaving is forced here, and it is an ordinary one: thread "near"
starts growing the chain; thread "far" looks up its next sector while "near"
is still at it (in a server, while "far" waits on a FAT page read); "near"
finishes its read; "far" then goes on from the sector it looked up -- which
"near" has meanwhile appended -- and appends it again. A chain holding a
sector twice maps every later offset of the stream to the wrong sector:
silently wrong pixels for the life of the reader.

On a reader that grows a chain under its lock, "far" cannot look anything up
while "near" grows it; "near"'s wait below times out and both go on.
"""

import threading

import numpy as np

from plexora.server.utils import formats
from plexora.server.utils.formats import cfb
from tests.formats_fixtures_more import ZviPlane, stream_bytes, write_cfb, write_zvi, zvi_plane


class _Interleave:
    """Holds the first FAT lookup of thread "near" until "far" has made its
    own, and "far"'s first lookup until "near"'s read has returned."""

    def __init__(self, cf):
        self.near_looking = threading.Event()
        self.far_looking = threading.Event()
        self.near_done = threading.Event()
        self._seen = set()
        original = cf._next

        def hooked(sector):
            name = threading.current_thread().name
            if name not in self._seen:
                self._seen.add(name)
                if name == "near":
                    self.near_looking.set()
                    self.far_looking.wait(3)
                elif name == "far":
                    self.far_looking.set()
                    self.near_done.wait(10)
            return original(sector)

        cf._next = hooked

    def run(self, near, far):
        errors = []

        def wrap(fn, done=None):
            def run():
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    if done is not None:
                        done.set()
            return run

        a = threading.Thread(target=wrap(near, self.near_done), name="near")
        b = threading.Thread(target=wrap(far), name="far")
        a.start()
        assert self.near_looking.wait(10), "the first read never looked up a FAT entry"
        b.start()
        a.join(30)
        b.join(30)
        assert not a.is_alive() and not b.is_alive(), "a read did not return"
        assert not errors, errors


def test_two_threads_growing_one_chain_read_the_right_bytes(tmp_path):
    data = stream_bytes(1_000_000, 7)
    write_cfb(tmp_path / "f.cfb", {"Big": data}, version=3, layout="contiguous")
    with open(tmp_path / "f.cfb", "rb") as handle:
        cf = cfb.CompoundFile(handle)
        try:
            entry = cf.find("Big")
            got = {}

            def read(name, offset):
                def run():
                    got[name] = cf.read(entry, offset, 4096)
                return run

            _Interleave(cf).run(read("near", 300_000), read("far", 900_000))
            chain = list(entry._chain)
            assert len(chain) == len(set(chain)), (
                f"the stream's chain holds {len(chain) - len(set(chain))} sectors twice: "
                f"two threads grew it at once")
            assert got["near"] == data[300_000:304_096]
            assert got["far"] == data[900_000:904_096], "the second thread read the wrong sectors"
            # and a later read, on one thread, is still right
            assert cf.read(entry, 950_000, 8192) == data[950_000:958_192]
        finally:
            cf.close()


def test_a_zvi_plane_first_read_by_two_threads_keeps_its_pixels(tmp_path):
    height, width = 1024, 1024
    planes = [ZviPlane(zvi_plane(z, 0, 0, height, width), z=z, c=0, name="DAPI")
              for z in range(2)]
    write_zvi(tmp_path / "s.zvi", planes, version=3, layout="contiguous")
    reader = formats.open_reader(tmp_path / "s.zvi")
    try:
        want = zvi_plane(1, 0, 0, height, width)
        got = {}

        def rows(name, y0, y1):
            def run():
                got[name] = reader.read(0, 0, y0, y1, 0, width, z=1)
            return run

        _Interleave(reader._cf).run(rows("near", 0, 64), rows("far", 900, 964))
        np.testing.assert_array_equal(got["near"], want[0:64])
        np.testing.assert_array_equal(got["far"], want[900:964],
                                      err_msg="rows 900-964 of z=1 read wrong")
        # the whole plane afterwards, on one thread: still the plane written
        np.testing.assert_array_equal(reader.read(0, 0, 0, height, 0, width, z=1), want,
                                      err_msg="z=1 reads wrong after its first concurrent reads")
    finally:
        reader.close()
