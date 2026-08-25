"""What has to move when an image's channels are renamed.

Applying a channel-name file used to reload the page, on the argument that
everything downstream reads names and patching each one in place was not worth
it. Two things were wrong with that.

The first is that the reload did not actually fix it. The project's SAVED
channel list holds names, and it is what the sidebar rebuilds its channel slots
from on every load -- so the reloaded page restored a slot for a channel that
no longer existed. It read as an extra marker matching nothing, and the stats
request it made came back as a StopIteration out of get_image_channel_stats:
`next(...)` over a channel list that no longer had that name.

The second is that a reload is expensive in the only currency that matters
here. It throws away the viewport, the active channels, the open tool and every
tuned contrast range -- to change what a few rows are called.

So: the names move together, everywhere, in place. This file pins both halves.

  server   the saved channel list moves with the rename, and a name that IS
           stale gets an answer rather than a traceback
  client   main.js's adoptChannelNames, and the two views that key state by
           channel name (tests/js/channel_rename_probe.mjs)

The dialog that collects the file is tests/test_channel_names_modal.py; the
route that applies it is tests/test_channel_names_upload.py.
"""

import io
import pickle
import shutil
import subprocess
from pathlib import Path

import pytest

import plexora
from plexora.server.models import data_model, database_model

# The same two-channel registered project the route tests use. Imported rather
# than copied: a second fixture that drifts from the first is a test suite
# quietly checking two different things.
from tests.test_channel_names_upload import _post, _register

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "channel_rename_probe.mjs"
CLIENT = REPO_ROOT / "plexora" / "client"


def source(*parts):
    return CLIENT.joinpath(*parts).read_text(encoding="utf-8")


def _save_channels(name, rows):
    """The saved channel list, written the way save_channel_list writes it."""
    database_model.save_list(database_model.ChannelList, datasource=name,
                             cells=pickle.dumps(rows, protocol=4))


def _saved_names(name):
    return [row["channel"] for row in data_model.get_saved_channel_list(name) or []]


# -- the saved channel list ---------------------------------------------------


def test_the_saved_channel_list_follows_the_rename(tmp_path, monkeypatch):
    """The bug, at its smallest. This list is what the sidebar restores from,
    so a name left behind here is a channel slot naming something the server
    does not have."""
    _register(tmp_path, monkeypatch)
    _save_channels("panel_sample", [
        {"channel": "MarkerA", "start": 0, "end": 255, "channel_active": True},
        {"channel": "MarkerB", "start": 0, "end": 255, "channel_active": False},
    ])

    response = _post(plexora.app.test_client(),
                     file=(io.BytesIO(b"marker\nDAPI\nCD3\n"), "panel.csv"))

    assert response.status_code == 200
    assert _saved_names("panel_sample") == ["DAPI", "CD3"]


def test_the_rest_of_a_saved_row_is_left_alone(tmp_path, monkeypatch):
    """Only the name changes. The colour and contrast the user set belong to
    the channel, which is the same channel it was before."""
    _register(tmp_path, monkeypatch)
    _save_channels("panel_sample", [
        {"channel": "MarkerA", "start": 12, "end": 400, "r": 255, "g": 0, "b": 0,
         "opacity": 1, "channel_active": True},
    ])

    _post(plexora.app.test_client(), file=(io.BytesIO(b"marker\nDAPI\nCD3\n"), "panel.csv"))

    row = data_model.get_saved_channel_list("panel_sample")[0]
    assert row["channel"] == "DAPI"
    assert (row["start"], row["end"], row["r"]) == (12, 400, 255)


def test_a_project_that_never_saved_a_channel_list_is_not_a_problem(tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch)
    response = _post(plexora.app.test_client(),
                     file=(io.BytesIO(b"marker\nDAPI\nCD3\n"), "panel.csv"))
    assert response.status_code == 200
    assert data_model.rename_saved_channels("panel_sample", {"DAPI": "CD4"}) is False


def test_a_row_naming_something_that_was_not_renamed_is_left_alone(tmp_path, monkeypatch):
    """A saved list can hold a name from further back than this rename --
    another rename, or a channel the project no longer has. Rewriting it to
    something arbitrary would be worse than leaving it stale."""
    _register(tmp_path, monkeypatch)
    _save_channels("panel_sample", [
        {"channel": "MarkerA", "channel_active": True},
        {"channel": "SomethingElse", "channel_active": True},
    ])

    data_model.rename_saved_channels("panel_sample", {"MarkerA": "DAPI"})

    assert _saved_names("panel_sample") == ["DAPI", "SomethingElse"]


# -- a name that is stale anyway ----------------------------------------------


def test_an_unknown_channel_says_which_one_and_where(monkeypatch):
    """`next(...)` with no default raised StopIteration: no message, neither
    the channel nor the project named, and a 500 at the other end."""
    monkeypatch.setattr(data_model, "config", {
        "tonsil": {"imageData": [{"name": "Area", "fullname": "Area"},
                                 {"name": "DAPI", "fullname": "DAPI"}]},
    })
    assert data_model.real_channel_index("DAPI", "tonsil") == 0, \
        "Area is not in the image, so it is not in zarray either"

    with pytest.raises(data_model.UnknownChannelError) as raised:
        data_model.real_channel_index("MarkerA", "tonsil")
    assert "MarkerA" in str(raised.value) and "tonsil" in str(raised.value)


def test_the_stats_call_raises_that_rather_than_stopping_iteration(monkeypatch):
    monkeypatch.setattr(data_model, "_ensure_loaded", lambda name: None)
    monkeypatch.setattr(data_model, "config", {
        "tonsil": {"imageData": [{"name": "DAPI", "fullname": "DAPI"}]},
    })
    with pytest.raises(data_model.UnknownChannelError):
        data_model.get_image_channel_stats("MarkerA", "tonsil")


@pytest.mark.parametrize("route", ["/get_image_channel_stats", "/get_channel_gmm"])
def test_a_stale_channel_name_gets_an_answer_not_a_traceback(monkeypatch, route):
    """Both routes are asked for a channel BY NAME, and a page open across a
    rename asks for the old one. That is a stale question, not a broken
    server, so it is a 404 with a sentence in it."""
    def raise_unknown(channel, datasource):
        raise data_model.UnknownChannelError(f"{channel!r} is not a channel of 'tonsil'.")

    monkeypatch.setattr(data_model, "get_image_channel_stats", raise_unknown)
    monkeypatch.setattr(data_model, "get_channel_gmm", raise_unknown)

    response = plexora.app.test_client().get(
        route, query_string={"channel": "MarkerA", "datasource": "tonsil"})

    assert response.status_code == 404
    body = response.get_json()
    assert body["unknown_channel"] is True and body["channel"] == "MarkerA"
    assert "MarkerA" in body["error"]


# -- the page ------------------------------------------------------------------


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def test_the_channel_list_carries_its_state_across(probe):
    assert "the row's element id follows the name" in probe, probe
    assert "...as does its slider" in probe, probe
    assert "...its fitted auto-level" in probe, probe
    assert "the label in the row is rewritten" in probe, probe
    assert "...and so is the name the colour picker reports against" in probe, probe


def test_what_is_keyed_by_index_is_not_touched(probe):
    """The argument the whole in-place path rests on: a rename reorders
    nothing, so tiles, colour connectors and range connectors are untouched."""
    assert "what is keyed by index is left alone" in probe, probe


def test_the_sidebars_slots_are_renamed_rather_than_rebuilt(probe):
    assert "the slot the user is looking at names the renamed channel" in probe, probe
    assert "every marker select is offered the new names" in probe, probe
    assert "a range the user tuned by hand follows its marker" in probe, probe
    assert "the option list is right before any slot is written back into it" in probe, probe


def test_two_channels_can_swap_names(probe):
    """A panel file with two markers the wrong way round. Moving key by key,
    the first move overwrites what the second is about to read."""
    assert "two channels that swap names do not eat each other" in probe, probe
    assert "a swap here does not eat itself either" in probe, probe


# -- the wiring ----------------------------------------------------------------


def test_the_page_takes_the_new_names_on_rather_than_reloading():
    main = source("src", "js", "main.js")
    assert "adoptChannelNames" in main
    assert "channelList.renameChannels(renames)" in main
    assert "__plexora.viewerSidebar?.renameChannels(renames)" in main


def test_nothing_is_handed_a_fresh_object_to_go_stale_on():
    """`config`, `imageChannels` and `dd` are held by reference by things that
    outlive the rename -- the viewer, the channel list, the sidebar, and every
    plugin's ctx.dataset, which reads all three live through getters. Replacing
    any of them leaves its holders on the old names, which is the same class of
    bug as not updating them at all (see refreshDataset's own note)."""
    main = source("src", "js", "main.js")
    body = main.split("adoptChannelNames(names) {", 1)[1].split("\n    };", 1)[0]
    for assignment in ("imageChannels =", "imageChannelsIdx =", "dd =", "config ="):
        assert assignment not in body, assignment
    assert "delete imageChannels[key]" in body
    assert "delete dd[fromFull]" in body


def test_the_reload_is_kept_for_the_one_case_that_cannot_be_patched():
    """A count that no longer matches means the page and the server disagree
    about what the image IS, and renaming by index would be guesswork."""
    main = source("src", "js", "main.js")
    assert "names.length !== channels.length) return false" in main
    channels = source("src", "js", "views", "channelList.js")
    assert "if (!applied) window.location.reload();" in channels
