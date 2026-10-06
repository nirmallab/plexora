"""Named selections: kept with the project, read back by name, handed over by reference.

What "these cells" means has had nowhere to live in Plexora -- a highlight
fades, a gate is a threshold. A selection is a name, the image, the cell ids
as the table names them, the shape it was drawn from and who made it, in core's
own plugin store, receipted and undoable like any other write.
"""

from __future__ import annotations

import pytest

import plexora
from plexora.agent import AgentSession, Policy, registry
from plexora.agent.core import selection
from plexora.server.models import viewer_sessions as vs
from tests.scimappro_fixtures import make_anndata_project

ROWS = Policy.from_flags(egress=["row_level"])


@pytest.fixture
def made(tmp_path):
    registry.discover([])
    return make_anndata_project(tmp_path)


def _invoke(name, arguments, policy=None, **kwargs):
    return registry.invoke(AgentSession(), name, arguments, policy=policy, **kwargs)


def test_set_get_list_delete_round_trip(made):
    ids = [c["id"] for c in made["cells"]][:5]
    stored = _invoke("set_selection", {"project": made["name"], "name": "tumour_edge",
                                       "cell_ids": ids, "shape": {"type": "Polygon",
                                                                  "coordinates": []}})
    assert stored["ok"], stored
    described = stored["result"]["selection"]
    assert described["n"] == 5 and described["image_id"] == "slide_A"
    assert described["ids_hash"] == selection.ids_hash(ids)
    assert "ids" not in described          # set answers the digest, not the ids
    receipt = stored["result"]["receipt"]
    assert receipt["undo_hint"]["tool"] == "delete_selection"
    assert receipt["persistent_state"] == "plugin_store:core"

    got = _invoke("get_selection", {"project": made["name"], "name": "tumour_edge"},
                  policy=ROWS)
    assert got["result"]["selection"]["ids"] == [str(i) for i in ids]
    listed = _invoke("list_selections", {"project": made["name"]})["result"]["selections"]
    assert [(s["name"], s["n"]) for s in listed] == [("tumour_edge", 5)]

    deleted = _invoke("delete_selection", {"project": made["name"], "name": "tumour_edge"})
    assert deleted["ok"] and deleted["result"]["receipt"]["undo_hint"]["tool"] == "set_selection"
    assert _invoke("list_selections", {"project": made["name"]})["result"]["selections"] == []


def test_reading_ids_is_row_level(made):
    _invoke("set_selection", {"project": made["name"], "name": "s", "cell_ids": [1, 2]})
    refused = _invoke("get_selection", {"project": made["name"], "name": "s"})
    assert refused["error"]["code"] == "permission_required"


def test_a_large_selection_comes_back_as_a_reference(made):
    ids = list(range(1, selection.INLINE_LIMIT + 2))
    _invoke("set_selection", {"project": made["name"], "name": "big", "cell_ids": ids})
    got = _invoke("get_selection", {"project": made["name"], "name": "big"},
                  policy=ROWS)["result"]["selection"]
    assert "ids" not in got and got["n"] == len(ids)
    assert got["ref"]["uri"] == f"plexora://project/{made['name']}/store/core/selections/big"
    small = _invoke("get_selection", {"project": made["name"], "name": "big",
                                      "inline_limit": 0}, policy=ROWS)
    assert "ids" not in small["result"]["selection"]


def test_replacing_a_selection_bumps_its_revision_and_can_be_undone(made):
    _invoke("set_selection", {"project": made["name"], "name": "s", "cell_ids": [1, 2]})
    again = _invoke("set_selection", {"project": made["name"], "name": "s", "cell_ids": [3]})
    assert again["result"]["selection"]["revision"] == 2
    hint = again["result"]["receipt"]["undo_hint"]
    assert hint["tool"] == "set_selection" and hint["arguments"]["cell_ids"] == ["1", "2"]


def test_duplicate_ids_are_kept_once_and_order_does_not_change_the_digest(made):
    _invoke("set_selection", {"project": made["name"], "name": "s", "cell_ids": [3, 1, 3]})
    record = selection.read_all(made["name"])["s"]
    assert record["cell_ids"] == ["3", "1"]
    assert selection.ids_hash(["1", "3"]) == selection.ids_hash(["3", "1"])


def test_the_digest_is_the_protocols(made):
    from spatialbridge.anndata import ids_hash

    ids = ["10", "2", "33"]
    assert selection.ids_hash(ids) == ids_hash(ids)


def test_a_bad_name_or_unknown_selection_is_refused(made):
    bad = _invoke("set_selection", {"project": made["name"], "name": "../x", "cell_ids": [1]})
    assert bad["error"]["code"] == "invalid_input"
    missing = _invoke("get_selection", {"project": made["name"], "name": "nope"}, policy=ROWS)
    assert missing["error"]["code"] == "invalid_input"
    unknown = _invoke("list_selections", {"project": "nope"})
    assert unknown["error"]["code"] == "unknown_project"


def test_show_points_at_the_cells_in_an_open_viewer(made, monkeypatch):
    import threading

    vs._reset_for_tests()
    monkeypatch.setitem(plexora.app.config, "PLEXORA_SERVING", True)
    view_id = vs.register(project=made["name"], client="browser")["view_id"]
    seen = []

    def tab():
        work = vs.wait_for_work(view_id, wait_s=5)
        for command in work["commands"]:
            seen.append(command)
            vs.ack(view_id, command["command_id"], status="done", result={"shown": 2})

    thread = threading.Thread(target=tab)
    thread.start()
    ids = [c["id"] for c in made["cells"]][:2]
    answer = _invoke("set_selection", {"project": made["name"], "name": "s",
                                       "cell_ids": ids, "show": True})
    thread.join(5)
    vs._reset_for_tests()
    assert answer["result"]["shown"]["shown"] == 2
    cells = seen[0]["arguments"]["cells"]
    assert seen[0]["type"] == "highlight_cells" and {c["id"] for c in cells} == set(ids)


def test_show_without_a_viewer_still_stores_it(made):
    answer = _invoke("set_selection", {"project": made["name"], "name": "s",
                                       "cell_ids": [1], "show": True})
    assert answer["ok"] and answer["result"]["shown"]["shown"] == 0
    assert "s" in selection.read_all(made["name"])


def test_the_bridge_collects_a_selection_with_its_ids(made):
    from spatialbridge.schema import Selection
    from spatialbridge.tools import BridgeTools

    from plexora.agent.bridge_provider import PlexoraProvider

    _invoke("set_selection", {"project": made["name"], "name": "edge", "cell_ids": [1, 2]})
    bridge = BridgeTools(PlexoraProvider(origin="bridge"))
    collected = bridge.call("bridge_collect", {"kind": "selection", "arguments": {
        "project": made["name"], "name": "edge"}})
    assert Selection.model_validate(collected).ids == ["1", "2"]
    # An outside agent through the same tool gets the reference, not the ids.
    outside = BridgeTools(PlexoraProvider(origin="mcp"))
    collected = outside.call("bridge_collect", {"kind": "selection", "arguments": {
        "project": made["name"], "name": "edge"}})
    assert "ids" not in collected and collected["ref"]["uri"].endswith("/selections/edge")
