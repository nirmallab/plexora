"""An agent session takes the table the viewer already holds -- only when it is
the very table the session would read -- and never makes the viewer load.

On the website a session's own read is a second parse of the CSV the page
parsed once for the viewer (1.5 s for LSP11385, before every first
calibration); on the desktop it is a second read of the file.
"""

import os
import time

from plexora.agent import AgentSession
from plexora.server.models import data_model
from plexora.server.providers.local import LocalTableProvider
from tests.agent_fixtures import make_synthetic_project


def _projects(tmp_path):
    first = make_synthetic_project(tmp_path, "one")
    make_synthetic_project(tmp_path, "two", seed=1)
    return first


def _own_copy(name):
    from plexora.server.models.project import Project

    record = Project.load(name)
    provider = LocalTableProvider(record.dataset, record.name)
    provider.load()
    return provider


def test_the_viewers_table_is_taken_when_it_is_the_same_one(tmp_path):
    _projects(tmp_path)
    data_model.load_datasource("one")
    viewer = data_model._providers.table
    held = AgentSession().data("one").table._provider._provider
    assert held is viewer
    assert held.frame is data_model.datasource
    # The answers are the ones a copy of its own gives.
    own = _own_copy("one")
    assert held.frame.equals(own.frame)
    assert held.describe() == own.describe()


def test_another_projects_table_is_read_and_the_viewer_keeps_its_own(tmp_path):
    _projects(tmp_path)
    data_model.load_datasource("two")
    held = AgentSession().data("one").table._provider._provider
    assert held is not data_model._providers.table
    assert data_model.is_loaded("two") and not data_model.is_loaded("one")
    assert held.frame.equals(_own_copy("one").frame)


def test_a_file_changed_since_the_viewer_read_it_is_read_again(tmp_path):
    made = _projects(tmp_path)
    data_model.load_datasource("one")
    path = made["csv_path"]
    with open(path, "a") as handle:
        handle.write("999,1,1,1,1\n")
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    held = AgentSession().data("one").table._provider._provider
    assert held is not data_model._providers.table
    assert held.frame.height == data_model.datasource.height + 1


def test_a_session_never_loads_the_viewers_project(tmp_path):
    _projects(tmp_path)
    AgentSession().data("one").table.describe()
    assert data_model._loaded_source is None


def test_a_taken_table_outlives_the_viewer_moving_on(tmp_path):
    _projects(tmp_path)
    data_model.load_datasource("one")
    session = AgentSession()
    data = session.data("one")
    frame = data.table._provider.frame
    time.sleep(0.01)
    data_model.load_datasource("two")
    # The session still answers from the table it took; the viewer's is
    # project two's now, and the session's identity check is unchanged, so
    # the held copy is kept rather than read again.
    again = session.data("one").table._provider
    assert again.frame is frame
    assert data_model.is_loaded("two")
