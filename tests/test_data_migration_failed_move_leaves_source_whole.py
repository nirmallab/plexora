"""A move that fails part way must leave the old data root as it was.

data_migration's module docstring promises that "a failed migration here leaves
both the data and the pointer exactly as they were, so the recovery is 'try
again', not 'find my projects'". The pointer half is kept (the setting is only
written on success); the data half is not: entries are moved one at a time in
name order, so `config.json` -- the project registry -- leaves the old root
before any project whose name sorts after it, and a failure on such a project
strands the registry in the new root while the setting still points at the
old one. Every entry that did get across is then a collision, so the retry the
docstring promises is refused.

The failure is forced on one entry (as a file held open on Windows, or a disk
filling part way, would) so the test runs the same on every platform.
"""

import time

from plexora.server.models import data_migration


def _make_root(root):
    root.mkdir(parents=True)
    (root / "config.json").write_text('{"alpha": {}, "zeta": {}}', encoding="utf-8")
    for name in ("alpha", "zeta"):
        (root / name).mkdir()
        (root / name / "image.tif").write_text(f"{name} pixels", encoding="utf-8")


def _wait_for_job(timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = data_migration.status()
        if state.get("status") != "running":
            return state
        time.sleep(0.02)
    raise AssertionError(f"migration did not finish: {data_migration.status()}")


def test_a_move_that_fails_part_way_leaves_the_old_root_whole_and_retryable(
        tmp_path, monkeypatch):
    """The registry and every project stay in the root the setting still
    points at, and pressing the button again is not refused."""
    source = tmp_path / "old"
    target = tmp_path / "new"
    _make_root(source)

    real = data_migration._transfer

    def fail_on_zeta(src, dst, name, mode):
        if name == "zeta":
            raise PermissionError("[WinError 32] zeta/image.tif is in use")
        return real(src, dst, name, mode)

    monkeypatch.setattr(data_migration, "_transfer", fail_on_zeta)
    recorded = []
    data_migration.reset()
    try:
        data_migration.start(source, target, data_migration.MODE_MOVE,
                             lambda: recorded.append(target))
        job = _wait_for_job()
    finally:
        data_migration.reset()

    assert job["status"] == "error"
    assert recorded == [], "the setting is not written after a failure"

    # The setting still points at `source`, so `source` must still be a
    # complete data root: its registry and every project in it.
    assert (source / "config.json").exists(), (
        f"the project registry left the root the setting points at; "
        f"moved before the failure: {job['migrated']}")
    for name in ("alpha", "zeta"):
        assert (source / name / "image.tif").read_text(encoding="utf-8") == \
            f"{name} pixels", f"{name} is no longer where the setting points"

    retry = data_migration.plan(source, target)
    assert retry.can_migrate, (
        f"the retry is refused: collisions {retry.collisions}, "
        f"problems {retry.problems}")
