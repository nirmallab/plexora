"""Column names reach the node exactly as the primary asked for them.

The table provider names columns in a query string (`?columns=`, `?names=`,
`?column=`). Written in raw, a `+` arrives as a space, an `&` starts a new
parameter and a `#` ends the URL, so `CD45+`, `Ki67 & PCNA` or `Panel#2`
read the wrong column on the node -- or none. Each request is parsed here the
way the node's Flask parses it, and must give back the names sent.
"""

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from plexora.server.providers import http, node as node_providers

NAMES = ["CD45+", "Ki67 & PCNA", "Panel#2", "a%20b", "plain"]


class _Captured(Exception):
    pass


@pytest.fixture
def provider(monkeypatch):
    sent = []

    def fake_request(_node, _method, path, **_kwargs):
        sent.append(path)
        raise _Captured()

    monkeypatch.setattr(http, "bytes_request", fake_request)
    monkeypatch.setattr(node_providers.node_registry, "address_generation",
                        lambda _name: 0)
    p = object.__new__(node_providers.NodeTableProvider)
    p._binding = SimpleNamespace(node="n", resource_id="cells")
    p._kind = "table"
    p._node = object()
    p._generation = 0
    p._spec = None
    p._spec_key = None
    p._loaded = None
    return p, sent


def _args(path):
    # What the node sees: werkzeug splits on & and decodes like parse_qs.
    return {k: v[0] for k, v in parse_qs(urlsplit(path).query,
                                         keep_blank_values=True).items()}


def test_all_cells_names_survive(provider):
    p, sent = provider
    with pytest.raises(_Captured):
        p.all_cells(NAMES, float)
    args = _args(sent[-1])
    assert args["columns"].split(",") == NAMES
    assert args["dtype"] == "float"


def test_filter_columns_names_survive(provider):
    p, sent = provider
    with pytest.raises(_Captured):
        p.filter_columns(NAMES)
    assert _args(sent[-1])["names"].split(",") == NAMES


def test_metadata_column_name_survives(provider):
    p, sent = provider
    for name in NAMES:
        with pytest.raises(_Captured):
            p.metadata_column(name)
        assert _args(sent[-1])["column"] == name


def test_geometry_names_survive(provider):
    p, sent = provider
    with pytest.raises(_Captured):
        p._fetch_geometry(NAMES, {"row_count": 3})
    assert _args(sent[-1])["columns"].split(",") == NAMES
