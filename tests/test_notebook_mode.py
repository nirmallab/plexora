"""Controls that act on the SERVER's machine, in a mode where that is not yours.

PLEXORA_NOTEBOOK_MODE has been set by the sidecar and the jupyter-server-proxy
entry point since they were written, and until now nothing read it. Two routes
needed to: Quit, which kills a process the kernel owns and tracks, and the
native file picker, which opens a dialog on a machine that in hosted mode has
no screen -- and so did not fail, it hung, holding a waitress thread until
something killed the subprocess.
"""

import pytest

import plexora
from plexora.server.utils import native_dialog


@pytest.fixture
def notebook_mode(monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_NOTEBOOK_MODE", True)
    return plexora.app.test_client()


@pytest.fixture
def desktop_mode(monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_NOTEBOOK_MODE", False)
    return plexora.app.test_client()


def test_shutdown_is_refused_in_notebook_mode(notebook_mode):
    """If this ever regresses the test suite dies with it, since os._exit
    takes the pytest process with it -- which is exactly the abruptness the
    route has in a notebook."""
    response = notebook_mode.post("/shutdown")

    assert response.status_code == 403
    assert "notebook" in response.get_json()["error"].lower()


def test_the_quit_button_is_not_rendered_in_notebook_mode(notebook_mode):
    assert b"nav_quit" not in notebook_mode.get("/").data


def test_the_quit_button_is_still_there_normally(desktop_mode):
    assert b"nav_quit" in desktop_mode.get("/").data


def test_browse_path_is_refused_before_it_can_open_a_dialog(notebook_mode, monkeypatch):
    monkeypatch.setattr(
        native_dialog, "browse_for_path",
        lambda **kwargs: pytest.fail("a native dialog must never be opened here"),
    )

    response = notebook_mode.post("/browse_path", json={"mode": "file"})

    assert response.status_code == 400
    assert "type the path" in response.get_json()["error"]


def test_browse_path_still_validates_its_arguments_normally(desktop_mode, monkeypatch):
    """The guard is added in front of the existing checks, not instead of
    them."""
    import plexora.server.routes.browse_routes as browse_routes

    monkeypatch.setattr(browse_routes, "browse_for_path",
                        lambda **kwargs: "/somewhere/chosen.csv")

    assert desktop_mode.post("/browse_path", json={"mode": "nonsense"}).status_code == 400
    assert desktop_mode.post("/browse_path", json={"filter": "nope"}).status_code == 400
    ok = desktop_mode.post("/browse_path", json={"mode": "file"})
    assert ok.status_code == 200
    assert ok.get_json()["path"] == "/somewhere/chosen.csv"


def test_the_flag_reaches_every_page_template():
    from plexora.server.routes.page_routes import template_data

    with plexora.app.test_request_context("/"):
        assert "notebook_mode" in template_data()


@pytest.mark.parametrize(("value", "expected"), [("1", True), ("", False)])
def test_the_docker_flag_can_come_from_the_environment(tmp_path, value, expected):
    """It used to come only from run.py's second positional argument, which the
    image's own CMD never passed -- so the container ran with the flag off and
    the import page gave host-shaped path hints.

    A subprocess for the same reason tests/_plugin_boundary_probe.py is one:
    create_app() runs once per interpreter, so the value it read at import
    cannot be re-read in this one.
    """
    import os
    import subprocess
    import sys

    env = {**os.environ, "PLEXORA_DATA_PATH": str(tmp_path)}
    if value:
        env["PLEXORA_DOCKER"] = value
    else:
        env.pop("PLEXORA_DOCKER", None)

    result = subprocess.run(
        [sys.executable, "-c",
         "import plexora; print(plexora.app.config['IS_DOCKER'])"],
        capture_output=True, text=True, env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)
