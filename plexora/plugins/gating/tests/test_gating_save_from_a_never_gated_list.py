"""A page that read a never-gated project still saves only what it changed.

The sidebar reads the saved list (`/get_saved_gating_list`) and names its
revision as `base` when it saves its whole list. For a project nobody has
gated yet that revision is "0": the empty list. If an agent writes gates while
the page holds that empty list, the page's next save must take only the rows
the page itself moved -- not put every marker it seeded at its full range back
over the agent's gates, nor drop the agent's rows the page never sent.
"""

import json

import pytest


@pytest.fixture
def client(tmp_path):
    import plexora
    from tests.autogate_fixtures import make_gating_project

    make_gating_project(tmp_path, name="gnever", grid=8, size=384)
    return plexora.app.test_client()


def _save(client, gates, **extra):
    return client.post("/plugins/gating/save_gating_list", data=json.dumps({
        "datasource": "gnever", "filter": {},
        "channels": {name: list(value) for name, value in gates.items()}, **extra}))


def _saved():
    from plexora.plugins.gating.server import model

    return {row["channel"]: row["gate_start"] for row in model.get_saved_gating_list("gnever")}


def test_a_page_that_read_an_empty_list_keeps_the_gates_an_agent_wrote_since(client):
    from plexora import api
    from plexora.plugins.gating.server import model

    # The page opens a project nobody has gated: an empty list, revision "0".
    page = client.get("/plugins/gating/get_saved_gating_list?datasource=gnever")
    assert page.status_code == 200
    base = page.headers["X-Gating-Revision"]
    assert base == "0"

    # An agent gates CD8 and CD20 while the page still holds the empty list.
    ds = api.project_data("gnever")
    full = model.default_ranges(ds)
    model.set_gate(ds, "CD8", 333.0, full["CD8"][1])
    model.set_gate(ds, "CD20", 444.0, full["CD20"][1])

    # The page moves CD3 and saves its whole list: CD8 seeded at its full
    # range by browsing to it, CD20 not sent at all.
    answer = _save(client, {"CD8": full["CD8"], "CD3": (95.0, full["CD3"][1])},
                   base=base)
    assert answer.status_code == 200

    saved = _saved()
    assert saved["CD3"] == 95.0, "the page's own change is taken"
    assert saved["CD8"] == 333.0, (
        f"the agent's CD8 gate was put back to the page's seeded full range "
        f"({saved['CD8']}, full range starts at {full['CD8'][0]})")
    assert saved.get("CD20") == 444.0, "the agent's CD20 gate, which the page never sent, is kept"
