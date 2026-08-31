"""Opening a project whose data is on a machine that is not connected.

The design has always been that this opens: the pixels or the table that ARE
here are still worth having, and refusing the whole project because one node is
asleep would make a closed laptop lid look like data loss. `/resource_status`
turns the absence into a sentence and `services/resourceStatus.js` puts it on
screen.

None of which happened, because nothing on the server had noticed. Two separate
silences, with the same symptom -- a project that opens onto nothing and says
nothing:

- **The node was still on the map but did not answer.** `NodeImageProvider.open`
  asks only for the OME header, which is optional, so it caught every
  `ResourceError` and returned no metadata. An unreachable machine came back
  from that as a project in perfect health, and the viewer pointed its tiles at
  a port nothing was listening on.
- **The node was not on the map at all**, which is what Disconnect leaves --
  the entry is forgotten on purpose while the project still names it. The
  registry's KeyError travelled out of `load_datasource` as a 500 on
  `/init_database`, after the page had already rendered.

The browser half is tests/js/resource_status_probe.mjs.
"""

from dataclasses import replace

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import data_model
from plexora.server.models import nodes as node_registry
from plexora.server.models import remotes as remote_store
from plexora.server.models.project import Project, ResourceBinding
from tests.helpers import ALL_CONFIRMED, project

#: A port nothing is listening on. The refusal is immediate, so these tests
#: cost a failed connect rather than a timeout.
DEAD = "http://127.0.0.1:41999"


@pytest.fixture
def client():
    return plexora.app.test_client()


def a_project(tmp_path, name="dark", node="o2", kind="image"):
    """A project registered here with one resource served from somewhere else.

    The image file is real even when the IMAGE is the remote one, because
    `load_datasource` is loud about a local image that has moved and this suite
    is about the remote absence, not that one.
    """
    path = tmp_path / f"{name}.ome.tif"
    # Two planes, not one: tifffile squeezes a single-channel stack to 2-D and
    # LocalImageProvider indexes the third axis.
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint16),
                     photometric="minisblack")
    project(name, channels=("A", "B"), confirmed=ALL_CONFIRMED,
            src=str(path), width=256, height=256).save()
    Project.load(name).with_resource(kind, ResourceBinding(
        kind=kind, provider="node", node=node, resource_id="slide")).save()
    return Project.load(name)


def map_a_node(name="o2", endpoint=DEAD, **extra):
    return node_registry.save(node_registry.Node(
        name=name, endpoint=endpoint, token="t", extra=extra))


# -- the two silences --------------------------------------------------------


def test_a_node_that_does_not_answer_is_reported_rather_than_ignored(
        client, tmp_path):
    """The tunnel died, or the laptop slept. The entry is still on the map and
    the address it names refuses the connection."""
    map_a_node()
    a_project(tmp_path)

    assert client.get("/init_database?datasource=dark").status_code == 200
    answer = client.get("/resource_status?datasource=dark").get_json()

    assert "image" in answer["unavailable"]
    assert answer["nodes"] == ["o2"]


def test_a_node_that_is_no_longer_on_the_map_is_a_sentence_not_a_500(
        client, tmp_path):
    """What Disconnect leaves behind: the entry is forgotten on purpose and the
    project still points at the name. This used to be a KeyError out of the
    registry, which reached the browser as a 500 on `/init_database` -- after
    the page had rendered, so the whole of it was a viewer that drew nothing."""
    a_project(tmp_path)  # deliberately no map entry

    assert client.get("/init_database?datasource=dark").status_code == 200
    answer = client.get("/resource_status?datasource=dark").get_json()

    assert "not connected" in answer["unavailable"]["image"]
    assert "Connect it" in answer["unavailable"]["image"]


def test_a_mask_on_a_machine_that_has_gone_is_reported_too(client, tmp_path):
    """`NodeSegmentationProvider.open` had nothing to load and so asked
    nothing, which made a mask on a machine that had gone indistinguishable
    from a mask that was fine. Every label tile 404'd and the project reported
    itself healthy."""
    map_a_node()
    a_project(tmp_path, kind="segmentation")

    client.get("/init_database?datasource=dark")
    answer = client.get("/resource_status?datasource=dark").get_json()

    assert "segmentation" in answer["unavailable"]


def test_an_ordinary_project_still_reports_nothing(client, tmp_path):
    """The common path, and the reason every page can ask this route
    unconditionally."""
    project("here", channels=("A",), confirmed=ALL_CONFIRMED).save()

    answer = client.get("/resource_status?datasource=here").get_json()

    assert answer["unavailable"] == {}
    assert answer["profiles"] == []


# -- who can bring it back ---------------------------------------------------


def test_the_profile_that_would_reconnect_it_is_named(client, tmp_path):
    """What the Connect button posts to. Resolved from the saved PROFILES
    rather than out of the `managed_by` marker, which names the node -- the two
    differ exactly when a profile sets `node_name`, and that is the case where
    reading a profile name out of the marker gives the wrong one."""
    remote_store.save(remote_store.Remote(
        name="HMS-O2", target="me@login.o2.hms.harvard.edu", node_name="o2"))
    a_project(tmp_path)
    client.get("/init_database?datasource=dark")

    answer = client.get("/resource_status?datasource=dark").get_json()

    assert answer["profiles"] == [{"node": "o2", "profile": "HMS-O2"}]


def test_a_node_somebody_registered_by_hand_is_not_offered_as_a_profile(
        client, tmp_path):
    """The same ownership test the health probe and `_forget_node` make. An
    entry the user typed points at an address they maintain; a saved profile
    that merely shares its name does not get to reopen it underneath them."""
    remote_store.save(remote_store.Remote(name="o2", target="me@elsewhere"))
    map_a_node()  # on the map with no managed_by marker: somebody's own
    a_project(tmp_path)
    client.get("/init_database?datasource=dark")

    answer = client.get("/resource_status?datasource=dark").get_json()

    assert answer["unavailable"]
    assert answer["profiles"] == []


def test_a_node_a_profile_manages_is_offered_even_once_it_is_forgotten(
        client, tmp_path):
    """A missing entry is the ORDINARY case here rather than a disqualification
    -- Disconnect removes it on purpose, which is why the project can be
    pointing at a name the registry no longer has."""
    remote_store.save(remote_store.Remote(name="o2", target="me@login"))
    a_project(tmp_path)  # no map entry at all
    client.get("/init_database?datasource=dark")

    answer = client.get("/resource_status?datasource=dark").get_json()

    assert answer["profiles"] == [{"node": "o2", "profile": "o2"}]


# -- and reading it again once it is back ------------------------------------


def test_reloading_is_the_only_thing_that_re_reads_a_project(client, tmp_path):
    """A browser reload cannot do this, and that is why the route exists.
    `_ensure_loaded` is keyed on the project NAME, so a project that opened
    with its image missing keeps that shape for the life of the process --
    reopening the page finds the name loaded and skips the read entirely."""
    a_project(tmp_path)
    client.get("/init_database?datasource=dark")
    assert client.get("/resource_status?datasource=dark").get_json()["unavailable"]

    # The machine comes back -- here, by the project being repointed at a real
    # file, which is the same thing from `load_datasource`'s side: a resource
    # that reads now and did not before.
    path = tmp_path / "slide.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint16),
                     photometric="minisblack")
    record = Project.load("dark").with_resource("image", None)
    record.patch(image=replace(record.image, src=str(path))).save()

    answer = client.post("/reload_datasource?datasource=dark").get_json()

    assert answer["success"] is True
    assert answer["unavailable"] == {}


def test_reloading_says_what_is_still_missing(client, tmp_path):
    """So the caller can tell "it worked" from "that machine is up and this
    resource is still not there" without reloading a page onto the same
    absence."""
    map_a_node()
    a_project(tmp_path)

    answer = client.post("/reload_datasource?datasource=dark").get_json()

    assert "image" in answer["unavailable"]


def test_reloading_an_unknown_project_is_a_404(client):
    assert client.post("/reload_datasource?datasource=nope").status_code == 404


# -- the node that went away AFTER the project opened ------------------------
#
# The third silence, and the one that survived the first two fixes. Both of
# those are about a LOAD that failed; this is about the load that never
# happened. `_ensure_loaded` is keyed on the project name, so a project opened
# while its node was up keeps exactly that shape for the life of the process --
# disconnect the node, open the project again, and the read is skipped, the
# load-time record is still clean, and the route said everything was fine while
# the viewer drew a blank page and whatever tiles were still in cache.


def test_a_node_disconnected_after_the_project_loaded_is_reported(
        client, tmp_path):
    map_a_node()
    a_project(tmp_path)
    client.get("/init_database?datasource=dark")
    # What a load with the node UP leaves behind: the project loaded, and the
    # record of that load is clean. Written rather than served, because what is
    # under test is what happens NEXT -- standing a real node up here would
    # cost a subprocess and change nothing about the answer.
    data_model._resource_errors.clear()
    assert client.get(
        "/resource_status?datasource=dark").get_json()["unavailable"] == {}

    node_registry.remove("o2")

    answer = client.get("/resource_status?datasource=dark").get_json()
    assert "not connected" in answer["unavailable"]["image"]
    assert answer["nodes"] == ["o2"]


def test_the_project_is_not_reloaded_to_find_that_out(client, tmp_path, capsys):
    """A registry read, not a probe and not a reload. The whole point of the
    check is that it costs nothing, so it can run on every open of every
    project -- and a reload here would be the expensive answer to a question a
    local file already answers."""
    map_a_node()
    a_project(tmp_path)
    client.get("/init_database?datasource=dark")
    node_registry.remove("o2")
    capsys.readouterr()

    client.get("/resource_status?datasource=dark")

    assert "Data loading done" not in capsys.readouterr().out


def test_a_disconnected_node_is_still_offered_as_a_profile(client, tmp_path):
    """The whole reason this matters: it is what puts the Connect button on the
    modal. A node absent from the registry is the ordinary post-Disconnect
    state, and the profile that would bring it back is saved right here."""
    remote_store.save(remote_store.Remote(
        name="hms", target="me@login.cluster.edu", node_name="o2"))
    map_a_node()
    a_project(tmp_path)
    client.get("/init_database?datasource=dark")

    node_registry.remove("o2")

    answer = client.get("/resource_status?datasource=dark").get_json()
    assert answer["profiles"] == [{"node": "o2", "profile": "hms"}]


def test_the_status_route_loads_the_project_it_is_asked_about(
        client, tmp_path):
    """The race that made the first project opened in a fresh server silent.

    The viewer asks for this while it is still setting itself up, and nothing
    it has called by then loads the project -- `/resource_routing` reads the
    project record only. So the route answered out of whatever project was
    loaded BEFORE, which on a fresh server is nothing at all: `source` did not
    match, `resource_unavailable` returned None for every kind, and a project
    with an unreachable node got a clean bill of health.
    """
    map_a_node()
    a_project(tmp_path)
    data_model._loaded_source = None  # a server that has opened nothing yet

    answer = client.get("/resource_status?datasource=dark").get_json()

    assert "image" in answer["unavailable"]


def test_a_project_that_cannot_open_at_all_still_gets_an_answer(
        client, tmp_path):
    """A local image that has moved is deliberately fatal -- there is nothing
    to draw and no useful degraded state. That must not take this route with
    it: answering 500 to "what is wrong?" is how a blank page stays
    unexplained."""
    a_project(tmp_path, name="moved", node="o2")
    record = Project.load("moved")
    record.patch(image=replace(record.image, src=str(tmp_path / "gone.tif"))).save()
    data_model._loaded_source = None

    answer = client.get("/resource_status?datasource=moved")

    assert answer.status_code == 200
    # The registry half still works, and it is the half that has something to
    # say about the node.
    assert "not connected" in answer.get_json()["unavailable"]["image"]


# -- and what the terminal hears while it is gone ----------------------------


def test_a_tile_from_a_vanished_node_is_a_503_not_a_500(client, tmp_path):
    """The viewer opens on what it already knows and asks for pixels anyway --
    it is meant to, since the project is still real and only the road to it is
    gone. Every one of those requests used to reach Flask as an unhandled
    exception, and an unhandled exception is a stack trace."""
    a_project(tmp_path)

    response = client.get("/generated/data/dark/dark_0/0/0_0.png")

    assert response.status_code == 503
    body = response.get_json()
    assert "not connected" in body["error"]
    # Named, because the answer is only actionable if you know which machine
    # to go and connect.
    assert body["node"] == "o2"


def test_the_first_tile_says_so(client, tmp_path, capsys):
    """Loud once, and about a request rather than about the load -- the load
    said its own piece minutes ago, and this is somebody looking at a viewer
    now and wondering why it is empty."""
    a_project(tmp_path)
    client.get("/init_database?datasource=dark")
    capsys.readouterr()  # everything the load itself had to say

    client.get("/generated/data/dark/dark_0/0/0_0.png")

    printed = capsys.readouterr().out
    assert "data node 'o2' is not connected" in printed
    assert "GET /generated/data/dark/dark_0/0/0_0.png" in printed


def test_the_rest_of_the_screenful_does_not(client, tmp_path, capsys):
    """What the flood actually was: OpenSeadragon asks one tile at a time, so
    a single viewport of a project on a disconnected node printed dozens of
    identical traces. The sentence is worth saying once."""
    a_project(tmp_path)
    client.get("/generated/data/dark/dark_0/0/0_0.png")
    capsys.readouterr()  # the load's own chatter, and the first sentence

    for tile in ("0_1", "1_0", "1_1", "2_0"):
        assert client.get(
            f"/generated/data/dark/dark_0/0/{tile}.png").status_code == 503

    assert "not connected" not in capsys.readouterr().out


def test_reloading_lets_it_be_said_again(client, tmp_path, capsys):
    """The quiet is keyed on the load, because reloading is the one thing that
    re-reads the bindings -- a silence that outlived it would hide the second
    absence as thoroughly as the first one was reported."""
    a_project(tmp_path)
    client.get("/generated/data/dark/dark_0/0/0_0.png")
    client.post("/reload_datasource?datasource=dark")
    capsys.readouterr()

    client.get("/generated/data/dark/dark_0/0/0_0.png")

    assert "data node 'o2' is not connected" in capsys.readouterr().out


def test_a_project_that_is_here_is_untouched_by_any_of_this(client, tmp_path):
    """The guard rail: nothing above may turn an ordinary failure into a 503.
    A tile nobody can name is still a plain error from the reader."""
    project("here", channels=("A",), confirmed=ALL_CONFIRMED).save()

    assert client.get(
        "/generated/data/here/here_0/0/0_0.png").status_code != 503
