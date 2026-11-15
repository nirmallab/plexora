"""A saved connection keeps `plugins=""` -- core only (audit nr-remotes-6).

`--plugins ""` means "no plugins" and is distinct from no flag at all (every
plugin). `from_dict` kept the empty string; `to_dict` dropped it with the other
falsy fields, so a saved core-only profile reconnected with every plugin.
"""
from plexora.server.models.remotes import Remote


def test_core_only_survives_a_round_trip():
    saved = Remote(name="o2", target="me@host", plugins="").to_dict()
    assert saved["plugins"] == ""
    assert Remote.from_dict("o2", saved).plugins == ""


def test_unset_plugins_is_still_left_out():
    saved = Remote(name="o2", target="me@host").to_dict()
    assert "plugins" not in saved
    assert Remote.from_dict("o2", saved).plugins is None


def test_named_plugins_round_trip():
    saved = Remote(name="o2", target="me@host", plugins="gating,qc").to_dict()
    assert Remote.from_dict("o2", saved).plugins == "gating,qc"
