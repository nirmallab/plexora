"""Remembering how a layer is drawn, and where it sits.

The Layers panel could always change a layer's colour, its opacity, its
contrast window and its place in the stack, and it wrote every one of them into
memory and none of them anywhere. So each choice survived exactly until the
page reloaded -- a control that works and then quietly forgets, which is worse
than one that refuses.

These two routes are what makes those choices statements about the SAMPLE. What
is pinned here is the merge (a colour change must not drop the window it was not
asked about), the partial order (an id the caller does not mention keeps its
place rather than falling off), and the refusals -- because the synthesized
layers have no record to write to and saying so is the difference between a
refusal and a silent no-op.
"""

import pytest

import plexora
from plexora.server.models.project import LayerSpec, Project

from tests import helpers
from tests.helpers import use_data_root


@pytest.fixture
def client(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    return plexora.app.test_client()


@pytest.fixture
def sample(client):
    """A project carrying two layers of its own, written straight to disk.

    Through the model rather than through `/import/sample`: what these routes
    do is read and rewrite a record, and importing one first would make every
    assertion here depend on the importer as well.
    """
    project = (helpers.project("demo")
               .with_layer(LayerSpec(id="he", kind="image", label="H&E",
                                     modality="he", src="/tmp/he.tif"))
               .with_layer(LayerSpec(id="tx", kind="points",
                                     modality="transcripts",
                                     src="/tmp/tx.parquet")))
    project.save()
    return project.name


def _layer(name, layer_id):
    return Project.find(name).layer(layer_id)


# -- what is drawn, and how ------------------------------------------------

def test_hiding_a_layer_survives_the_reload(client, sample):
    response = client.patch(f"/project/{sample}/layers/he", json={"visible": False})
    assert response.status_code == 200
    assert _layer(sample, "he").visible is False
    # And on the wire, which is what the viewer actually reads back.
    entry = _layer(sample, "he").to_entry()
    assert entry["visible"] is False


def test_showing_it_again_leaves_no_trace(client, sample):
    client.patch(f"/project/{sample}/layers/he", json={"visible": False})
    client.patch(f"/project/{sample}/layers/he", json={"visible": True})
    assert _layer(sample, "he").visible is True
    # `True` is the default and an explicit `true` in every entry is noise in a
    # file people read -- the same rule LayerSpec.to_entry has always followed.
    assert "visible" not in _layer(sample, "he").to_entry()


def test_render_is_merged_and_not_replaced(client, sample):
    """The whole reason this is a PATCH. A card changing the colour must not
    silently drop the window somebody set a minute earlier."""
    client.patch(f"/project/{sample}/layers/he",
                 json={"render": {"range": [100, 900], "channelIndex": 2}})
    client.patch(f"/project/{sample}/layers/he", json={"render": {"color": "#ff8800"}})
    render = _layer(sample, "he").render
    assert render == {"range": [100, 900], "channelIndex": 2, "color": "#ff8800"}


def test_a_null_removes_a_key_rather_than_storing_it(client, sample):
    """How "use the file's own window" is said. Storing `null` would make
    `parse_style` read it as a number and fail differently every time."""
    client.patch(f"/project/{sample}/layers/he",
                 json={"render": {"range": [100, 900], "color": "#ff8800"}})
    client.patch(f"/project/{sample}/layers/he", json={"render": {"range": None}})
    assert _layer(sample, "he").render == {"color": "#ff8800"}


def test_the_opacity_is_stored_like_any_other_presentation_fact(client, sample):
    """`render` is the per-kind bag the server never acts on, so a new control
    is a new key and not a change here. This asserts that stays true."""
    client.patch(f"/project/{sample}/layers/he", json={"render": {"opacity": 0.35}})
    assert _layer(sample, "he").render["opacity"] == 0.35


def test_a_layers_whole_channel_list_is_stored_under_render(client, sample):
    """What a registered layer's channel panel saves.

    THE WHOLE LIST every time, because `render` merges one key deep: a partial
    list IS the new list. `render` is the unmodelled bag, so this needs no
    `LayerSpec` change -- which is exactly the property being asserted, since a
    value that had to be modelled would have to be modelled again for the next
    control.
    """
    channels = [
        {"index": 0, "name": "mx_0", "color": "#2388ff", "range": [120, 4001]},
        {"index": 3, "name": "mx_3", "color": "#ff2d2d", "range": [8, 900]},
    ]
    client.patch(f"/project/{sample}/layers/he", json={"render": {"channels": channels}})

    stored = _layer(sample, "he").render["channels"]
    assert stored == channels
    # And on the wire, which is what the panel reads back on the next open.
    assert _layer(sample, "he").to_entry()["render"]["channels"] == channels


def test_a_later_opacity_change_does_not_drop_the_channel_list(client, sample):
    """The two controls are on the same card and write the same bag. An opacity
    patch that replaced `render` would silently forget every colour and window
    the user had set."""
    channels = [{"index": 0, "name": "mx_0", "color": "#ffffff", "range": [0, 65535]}]
    client.patch(f"/project/{sample}/layers/he", json={"render": {"channels": channels}})
    client.patch(f"/project/{sample}/layers/he", json={"render": {"opacity": 0.35}})

    render = _layer(sample, "he").render
    assert render["channels"] == channels and render["opacity"] == 0.35


def test_an_empty_channel_list_is_stored_rather_than_treated_as_absent(client, sample):
    """"Every channel switched off" and "never styled" are different states:
    the second auto-levels the first channel on the next open, and the first
    must not."""
    client.patch(f"/project/{sample}/layers/he", json={"render": {"channels": []}})
    assert _layer(sample, "he").render["channels"] == []


def test_a_patch_that_changes_nothing_is_refused(client, sample):
    response = client.patch(f"/project/{sample}/layers/he", json={})
    assert response.status_code == 400


def test_render_has_to_be_an_object(client, sample):
    response = client.patch(f"/project/{sample}/layers/he", json={"render": [1, 2]})
    assert response.status_code == 400


def test_a_synthesized_layer_is_refused_rather_than_ignored(client, sample):
    """The mask's opacity belongs to the Cells footer and the image is the
    ground. Storing either here would be a second answer that nothing reads."""
    for layer_id in ("__image__", "__mask__", "__centroids__"):
        response = client.patch(f"/project/{sample}/layers/{layer_id}",
                                json={"visible": False})
        assert response.status_code == 400, layer_id


def test_an_unknown_layer_is_a_404(client, sample):
    assert client.patch(f"/project/{sample}/layers/nope",
                        json={"visible": False}).status_code == 404


def test_an_unknown_project_is_a_404(client):
    assert client.patch("/project/nothing-here/layers/he",
                        json={"visible": False}).status_code == 404


# -- where they sit --------------------------------------------------------

def test_the_order_is_stored_bottom_first(client, sample):
    response = client.put(f"/project/{sample}/layers/order",
                          json={"ids": ["tx", "he"]})
    assert response.status_code == 200
    assert [layer.id for layer in Project.find(sample).spatial_layers] == ["tx", "he"]


def test_an_unmentioned_layer_keeps_its_place_underneath(client, sample):
    """The same partial-order rule `LayerStack.setOrder` follows on the client.
    The two have to agree: the panel sends the order it is SHOWING, and a
    stored order that dropped what it had no card for would lose it."""
    client.put(f"/project/{sample}/layers/order", json={"ids": ["he"]})
    assert [layer.id for layer in Project.find(sample).spatial_layers] == ["tx", "he"]


def test_ordering_a_synthesized_layer_is_refused(client, sample):
    """Where the mask and the centroids composite is `all_layers`' answer
    every time it is read. A caller naming one believes it can move something
    it cannot. The reference image is the one exception -- see below."""
    for reserved in ("__mask__", "__centroids__"):
        response = client.put(f"/project/{sample}/layers/order",
                              json={"ids": [reserved, "he"]})
        assert response.status_code == 400
    assert [layer.id for layer in Project.find(sample).spatial_layers] == ["he", "tx"]


def test_the_reference_image_can_be_ordered_with_the_rest(client, sample):
    """The reference image's card can be dragged now, so the order it sends
    names it. What is kept is a depth rather than a place in a list it is not
    in: it is synthesized, and every other layer's registration is expressed
    against it."""
    response = client.put(f"/project/{sample}/layers/order",
                          json={"ids": ["he", "__image__", "tx"]})
    assert response.status_code == 200
    assert response.get_json()["imageDepth"] == 1
    assert [layer.id for layer in Project.find(sample).all_layers][:2] \
        == ["he", "__image__"]


def test_the_reference_image_stores_how_it_is_drawn(client, sample):
    """The ground it composites onto. Its channels carry coverage in their
    alpha now, so what shows through where they have no signal is a choice --
    and a choice that survived only until a reload would be worse than none."""
    response = client.patch(f"/project/{sample}/layers/__image__",
                            json={"render": {"background": "#ffffff"}})
    assert response.status_code == 200
    assert Project.find(sample).reference_layer.render["background"] == "#ffffff"
    # Merged, like every other layer's render.
    client.patch(f"/project/{sample}/layers/__image__",
                 json={"render": {"somethingElse": 1}})
    stored = Project.find(sample).reference_layer.render
    assert stored["background"] == "#ffffff" and stored["somethingElse"] == 1
    # And null removes one, which is how "use the default" is said.
    client.patch(f"/project/{sample}/layers/__image__",
                 json={"render": {"background": None}})
    assert "background" not in Project.find(sample).reference_layer.render


def test_the_reference_image_does_not_store_an_eye(client, sample):
    """It is the one layer whose absence leaves the viewer with no world at
    all, so a project that remembered it switched off would open on nothing."""
    assert client.patch(f"/project/{sample}/layers/__image__",
                        json={"visible": False}).status_code == 400


def test_the_mask_still_stores_nothing(client, sample):
    """The reference image being allowed through is not the reserved ids being
    allowed through. The mask's opacity is the Cells footer's."""
    for reserved in ("__mask__", "__centroids__"):
        assert client.patch(f"/project/{sample}/layers/{reserved}",
                            json={"render": {"background": "#fff"}}).status_code == 400


def test_ordering_an_unknown_layer_is_refused(client, sample):
    assert client.put(f"/project/{sample}/layers/order",
                      json={"ids": ["ghost"]}).status_code == 400


def test_ids_has_to_be_a_list(client, sample):
    assert client.put(f"/project/{sample}/layers/order",
                      json={"ids": "he"}).status_code == 400


def test_a_layer_called_order_still_reaches_the_patch_route(client, sample):
    """Flask matches in registration order and `<path:layer_id>` would swallow
    `/layers/order` if it were registered first. Both routes exist, so this is
    the test that says which."""
    Project.mutate(sample, lambda current: current.with_layer(
        LayerSpec(id="order", kind="image", label="Order", src="/tmp/o.tif")))
    response = client.patch(f"/project/{sample}/layers/order",
                            json={"render": {"color": "#123456"}})
    assert response.status_code == 200
    assert _layer(sample, "order").render == {"color": "#123456"}
    # And the PUT still reaches the ordering route rather than that layer.
    assert client.put(f"/project/{sample}/layers/order",
                      json={"ids": ["order", "he", "tx"]}).status_code == 200
    assert [layer.id for layer in Project.find(sample).spatial_layers] \
        == ["order", "he", "tx"]


def test_what_the_viewer_reads_back_carries_the_order(client, sample):
    """The round trip that matters: the panel stores an order and `/config`'s
    layer list -- which is what `ImageViewer.syncLayers` adopts -- gives it
    back with the synthesized layers around it."""
    client.put(f"/project/{sample}/layers/order", json={"ids": ["tx", "he"]})
    ids = [layer.id for layer in Project.find(sample).all_layers]
    assert ids[0] == "__image__"
    assert ids[-2:] == ["tx", "he"]
