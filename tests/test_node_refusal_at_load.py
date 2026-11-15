"""A node that refuses this server's token must not open as a healthy project.

`load_datasource` deliberately lets a node's REFUSAL out (data_model's
`attempt` catches only unreachability): a refused token has a fix -- register
the node again -- and swallowing it turns a broken project into an empty one.
The table provider let it out, but the image and the mask `open()` both read
every `ResourceError` as "this file has no header / is still converting" and
returned as if all was well. The project opened onto a blank viewer with
nothing recorded, and every tile failed one 500 at a time.
"""

from types import SimpleNamespace

import pytest

from plexora.server import providers
from plexora.server.models import data_model
from plexora.server.providers import http, node as node_providers


def _response(status, body=b""):
    return SimpleNamespace(status=status, headers={}, data=body)


def test_a_token_refusal_is_its_own_kind_of_resource_error():
    node = SimpleNamespace(name="lab")
    with pytest.raises(providers.NodeRefused) as raised:
        http._check(node, _response(401), expected_api=None)
    assert isinstance(raised.value, providers.ResourceError)
    assert "token" in str(raised.value)


def _provider(cls, monkeypatch, error):
    def refuse(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(http, "json_request", refuse)
    monkeypatch.setattr(node_providers.node_registry, "address_generation",
                        lambda _name: 0)
    p = object.__new__(cls)
    p._binding = SimpleNamespace(node="lab", resource_id="r1")
    p._node = SimpleNamespace(name="lab")
    p._generation = 0
    p._volumetric = False
    return p


@pytest.mark.parametrize("cls", [node_providers.NodeImageProvider,
                                 node_providers.NodeSegmentationProvider])
def test_open_lets_a_refusal_out(cls, monkeypatch):
    error = providers.NodeRefused("data node 'lab' refused this server's token.")
    with pytest.raises(providers.NodeRefused):
        _provider(cls, monkeypatch, error).open()


@pytest.mark.parametrize("cls", [node_providers.NodeImageProvider,
                                 node_providers.NodeSegmentationProvider])
def test_open_lets_a_version_mismatch_out(cls, monkeypatch):
    error = providers.NodeVersionMismatch(3, 2, node="lab")
    with pytest.raises(providers.NodeVersionMismatch):
        _provider(cls, monkeypatch, error).open()


@pytest.mark.parametrize("cls", [node_providers.NodeImageProvider,
                                 node_providers.NodeSegmentationProvider])
def test_open_still_shrugs_off_an_unhelpful_answer(cls, monkeypatch):
    # "no such resource" and the like stay the tile path's to report.
    error = providers.ResourceError("data node 'lab' does not serve that resource")
    _provider(cls, monkeypatch, error).open()


def test_an_image_refusal_is_recorded_as_inaccessible_not_corrupt():
    status, _ = data_model.classify_image_error(
        providers.NodeRefused("data node 'lab' refused this server's token."))
    assert status == "inaccessible"
