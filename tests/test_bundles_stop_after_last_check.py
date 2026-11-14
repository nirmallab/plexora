"""A Stop that reaches an import after its registry write
(server/models/bundles.py `_apply`): the projects are already in the registry,
so the job must not report "cancelled". On the website a "cancelled" status
makes the page take back every file it placed (bundleWeb.js `follow` ->
`takeBack`) and the dialog says "Nothing was added", while the projects stay
registered without their files."""

import threading
import time

from plexora.server.models import bundles, layer_jobs
from plexora.server.models.project import Project
from tests.test_bundles import _members, project  # noqa: F401 -- fixture


def _wait(token, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = bundles.job_status(token)
        if status["status"] != "pending":
            return status
        time.sleep(0.05)
    raise AssertionError("the job did not finish")


def test_a_stop_after_the_registry_write_does_not_report_the_import_cancelled(project, tmp_path,  # noqa: F811
                                                                              monkeypatch):
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out)
    pid = _members(out)[1]["projects"][0]["id"]

    stop = threading.Event()
    real_start_task = layer_jobs.start_task

    def start_task(*args, **kwargs):
        kwargs["cancel"] = stop
        return real_start_task(*args, **kwargs)

    monkeypatch.setattr(layer_jobs, "start_task", start_task)

    pressed = []
    real_record_revision = bundles._record_revision

    def record_revision(*args, **kwargs):
        # The user's Stop, landing while the import notes each project's
        # revision: after the registry write, before the job returns.
        stop.set()
        pressed.append(True)
        return real_record_revision(*args, **kwargs)

    monkeypatch.setattr(bundles, "_record_revision", record_revision)

    status = _wait(bundles.start_import(out, choices={pid: "keep_both"})["job"])
    assert pressed, "the Stop never landed after the registry write"

    assert "slide_2" in Project.load_all(), "the import did not reach its registry write"
    # The import landed, so the job says so: a "cancelled" here makes the
    # website's page delete the files of a project that stays registered.
    assert status["status"] == "ready", (status["status"], status["result"])
