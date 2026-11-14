"""The 3-D copy's staleness key of an OME-Zarr stack does not walk its chunks
(volume/bricks.py `source_key`, called by `store_for`).

`data_model._with_bricks` builds the key while `load_datasource` holds the load
lock, on every load of a large deep stack. An OME-Zarr store is a folder of
one file per chunk -- hundreds of thousands for a deep z-stack chunked one
plane at a time -- so a key that stats every file blocks every tile and table
request for tens of seconds each time the project opens. What the key needs
to notice (a level rewritten, the store replaced) is in the store's metadata
documents and its own folder, whose number does not grow with the chunks.
"""

import os
from types import SimpleNamespace

from plexora.server.volume import bricks
from tests import volume_fixtures as vf


def _counting(monkeypatch):
    """Count the file-system entries a call looks at: os.stat calls (which
    Path.stat makes) and the entries os.scandir yields (which os.walk uses)."""
    seen = {"n": 0}
    real_stat, real_scandir = os.stat, os.scandir

    def stat(*args, **kwargs):
        seen["n"] += 1
        return real_stat(*args, **kwargs)

    class _Scan:
        def __init__(self, inner):
            self._inner = inner

        def __iter__(self):
            for entry in self._inner:
                seen["n"] += 1
                yield entry

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def close(self):
            self._inner.close()

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "scandir", lambda *a, **k: _Scan(real_scandir(*a, **k)))
    return seen


def _key_cost(tmp_path, monkeypatch, name, depth):
    path, _ = vf.write_ngff_volume(tmp_path / f"{name}.ome.zarr", channels=2, depth=depth,
                                   height=256, width=256, levels=2, chunks_z=1)
    chunk_files = sum(len(files) for _r, _d, files in os.walk(path))
    monkeypatch.setattr("plexora.paths.derived_root", lambda _name, *a, **k: tmp_path / name)
    source = SimpleNamespace(path=str(path), volume=None)
    with monkeypatch.context() as m:
        seen = _counting(m)
        store = bricks.store_for(name, source)
    assert store.key is not None
    return seen["n"], chunk_files


def test_the_staleness_key_of_an_ome_zarr_stack_does_not_grow_with_its_chunk_count(
        tmp_path, monkeypatch):
    shallow, shallow_files = _key_cost(tmp_path, monkeypatch, "shallow", depth=8)
    deep, deep_files = _key_cost(tmp_path, monkeypatch, "deep", depth=96)
    # Same levels, same channels, twelve times the planes and so the chunks.
    assert deep_files > 10 * shallow_files
    assert deep <= shallow + 8, (
        f"keying a store of {deep_files} files looked at {deep} entries, "
        f"one of {shallow_files} files at {shallow}: the key walks every chunk")


def test_the_staleness_key_still_changes_when_a_level_or_the_store_is_rewritten(tmp_path):
    path, _ = vf.write_ngff_volume(tmp_path / "s.ome.zarr", channels=2, depth=8,
                                   height=128, width=128, levels=2, chunks_z=1)
    later = 2_000_000_000 * 10**9  # well after anything the writer stamped

    def stamp_everything(ns):
        for root, dirs, files in os.walk(path):
            for name in files + dirs:
                os.utime(os.path.join(root, name), ns=(ns, ns))
        os.utime(path, ns=(ns, ns))

    stamp_everything(later)
    before = bricks.source_key(path)
    level_meta = path / "1" / ".zarray"
    level_meta.write_bytes(level_meta.read_bytes())
    os.utime(level_meta, ns=(later + 10**9, later + 10**9))
    assert bricks.source_key(path) != before

    stamp_everything(later + 2 * 10**9)
    before = bricks.source_key(path)
    os.utime(path, ns=(later + 3 * 10**9, later + 3 * 10**9))
    root_meta = path / ".zattrs"
    os.utime(root_meta, ns=(later + 3 * 10**9, later + 3 * 10**9))
    assert bricks.source_key(path) != before
