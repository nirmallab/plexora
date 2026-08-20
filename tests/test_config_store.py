"""How config.json is read and written, as opposed to what it contains.

Reading it used to be `json.load(open(path))` in half a dozen places and
writing it `open(path, "w")` in four, which meant every write opened a window
where the file on disk was zero bytes. Waitress serves requests on several
threads and the segmentation job runs on another, so a read landing inside that
window is not hypothetical: importing a CSV would fail the *next* page with
"Expecting value: line 1 column 1 (char 0)" -- a stack trace pointing at a
reader that had done nothing wrong.

These pin the two properties that fix it: a write is one step as far as any
reader can see, and every reader goes through the same lock.
"""

import json
import threading

import pytest

from plexora.server.models.project import (
    Project,
    read_config,
    write_config,
)


@pytest.fixture
def config_path(tmp_path):
    return tmp_path / "config.json"


def _entry(size):
    """A project entry big enough that serializing it is not instantaneous --
    the race needs a write window wide enough to be caught."""
    return {"channelFile": "/x.ome.tif", "columns": [f"marker_{i}" for i in range(size)]}


def test_a_missing_config_reads_as_no_projects(config_path):
    assert read_config(config_path) == {}


def test_an_empty_config_reads_as_no_projects(config_path):
    """A zero-byte file is what a crash (or the old in-place writer) leaves
    behind. There is nothing in it to lose, so it is not worth an exception."""
    config_path.write_text("", encoding="utf-8")

    assert read_config(config_path) == {}


def test_a_corrupt_config_still_raises(config_path):
    """Tolerating an empty file must not turn into tolerating a damaged one:
    reading a half-written config as {} would let the next save delete every
    project in it."""
    config_path.write_text('{"demo": ', encoding="utf-8")

    with pytest.raises(ValueError):
        read_config(config_path)


def test_a_write_lands_whole_or_not_at_all(config_path):
    """The regression. A reader hammering the file while a writer rewrites it
    must never see a truncated one -- before the atomic rename it saw an empty
    file often enough to break an import on the first try."""
    write_config(config_path, {"demo": _entry(2000)})

    stop = threading.Event()
    failures = []

    def reader():
        while not stop.is_set():
            try:
                # Deliberately NOT read_config(): this reads the way an
                # unlucky external process would, without the in-process lock,
                # so what is being tested is the file on disk rather than the
                # lock that happens to serialize the two threads here.
                text = config_path.read_text(encoding="utf-8")
                if text:
                    json.loads(text)
            except PermissionError:
                # Windows refuses to open a file mid-rename. That is the window
                # read_config() retries past; the test below covers it, and it
                # is not a partial read.
                pass
            except FileNotFoundError:
                failures.append("config.json vanished mid-write")
            except ValueError as exc:
                failures.append(f"read a partial config: {exc}")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for i in range(50):
            write_config(config_path, {f"demo_{i}": _entry(2000)})
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not failures, failures[:3]


def test_concurrent_writers_do_not_lose_each_others_projects(config_path):
    """Two threads saving different projects is the import flow's normal state:
    the request saves the record while the segmentation job patches the mask
    fields. Both writes have to survive."""
    write_config(config_path, {})
    barrier = threading.Barrier(4)

    def save(name):
        barrier.wait()
        Project(name=name).save(data_root=config_path.parent)

    threads = [threading.Thread(target=save, args=(f"p{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(read_config(config_path)) == ["p0", "p1", "p2", "p3"]


def test_a_reader_waits_out_a_rename_rather_than_failing(monkeypatch, config_path):
    """The other half of writing by rename: on Windows, opening a file while it
    is being replaced fails outright. A reader in another process (a notebook
    sidecar beside a CLI server) would otherwise see that as a hard error on a
    file that is perfectly intact a millisecond later."""
    write_config(config_path, {"demo": _entry(10)})
    real_read_text = type(config_path).read_text
    calls = []

    def flaky_read_text(self, *args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "being replaced")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(config_path), "read_text", flaky_read_text)

    assert read_config(config_path) == {"demo": _entry(10)}
    assert len(calls) == 3


def test_a_reader_gives_up_on_a_lock_that_is_not_transient(monkeypatch, config_path):
    """Retrying must not turn a genuinely unreadable file into a hang or, worse,
    into an empty config that the next save would write back over the real one."""
    write_config(config_path, {"demo": _entry(10)})
    monkeypatch.setattr(
        type(config_path), "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")),
    )

    with pytest.raises(PermissionError):
        read_config(config_path)


def test_a_write_leaves_no_temp_file_behind(config_path):
    write_config(config_path, {"demo": _entry(10)})

    assert [p.name for p in config_path.parent.iterdir()] == ["config.json"]
