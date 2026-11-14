"""A cache filled from the table as it was before a reload must not outlive it.

The gate columns (`get_filter_columns`) and the `dd` description
(`get_datasource_description`) are cached per project and read without
`load_lock`. A reload of the same project -- the log1p switch, an agent's
set_expression_source, /reload_datasource after re-exporting the CSV -- clears
those caches. A request that started computing from the old table before the
reload, and finishes after the clear, must not put the old table's answer back:
every gate tick and histogram after that would describe values the loaded
table no longer holds.
"""
import threading

import numpy as np

from plexora.server.models import data_model

from tests.autogate_fixtures import make_gating_project


def _hold_in(monkeypatch, attr, worker_name):
    """Wrap data_model.<attr> so the named thread stops after computing, until
    released. Every other thread (the reload itself) runs it straight through."""
    computed, release = threading.Event(), threading.Event()
    real = getattr(data_model, attr)

    def held(*args, **kwargs):
        out = real(*args, **kwargs)
        if threading.current_thread().name == worker_name:
            computed.set()
            assert release.wait(20), "the test never released the held computation"
        return out

    monkeypatch.setattr(data_model, attr, held)
    return computed, release


def _switch_to_log1p_while_held(name, call, computed, release, worker_name):
    from plexora.api import features

    result = {}
    worker = threading.Thread(target=lambda: result.update(value=call()),
                              name=worker_name, daemon=True)
    worker.start()
    assert computed.wait(20), "the request never reached its computation"
    # The table changes under the request: the user turns log1p on.
    _, after, changed = features.apply(name, features_log=True)
    assert changed and after["features_log"] is True
    release.set()
    worker.join(20)
    assert not worker.is_alive()
    return result


def test_gate_columns_computed_before_a_log1p_reload_are_not_served_after_it(
        tmp_path, monkeypatch):
    info = make_gating_project(tmp_path, name="stale_gate_cols", mask=False)
    name = info["name"]
    data_model.load_datasource(name, wait_for_table=True)
    raw_max = float(data_model.get_datasource_df()["CD3"].max())

    worker_name = "audit-gating-tick"
    computed, release = _hold_in(monkeypatch, "_filter_columns_from_frame", worker_name)
    # A gating tick asks for CD3's column while the reload lands.
    _switch_to_log1p_while_held(
        name, lambda: data_model.get_filter_columns(name, ["CD3"]),
        computed, release, worker_name)

    logged_max = float(data_model.get_datasource_df()["CD3"].max())
    assert logged_max < raw_max  # the reload did read through log1p

    served = float(np.nanmax(data_model.get_filter_columns(name, ["CD3"])["CD3"]))
    assert np.isclose(served, logged_max), (
        f"after the log1p reload the gate columns still hold the raw values "
        f"(CD3 max {served:.3f}; the loaded table's max is {logged_max:.3f}, "
        f"the raw table's was {raw_max:.3f})")


def test_description_computed_before_a_log1p_reload_is_not_served_after_it(
        tmp_path, monkeypatch):
    info = make_gating_project(tmp_path, name="stale_describe", mask=False)
    name = info["name"]
    data_model.load_datasource(name, wait_for_table=True)
    raw_max = float(data_model.get_datasource_df()["CD3"].max())

    worker_name = "audit-describe"
    computed, release = _hold_in(monkeypatch, "_describe_frame", worker_name)
    # The viewer's (or the warm-up's) first description is being computed.
    _switch_to_log1p_while_held(
        name, lambda: data_model.get_datasource_description(name),
        computed, release, worker_name)

    logged_max = float(data_model.get_datasource_df()["CD3"].max())
    assert logged_max < raw_max

    served = float(data_model.get_datasource_description(name)["CD3"]["max"])
    assert np.isclose(served, logged_max, rtol=1e-4), (
        f"after the log1p reload the description still describes the raw table "
        f"(CD3 max {served:.3f}; the loaded table's max is {logged_max:.3f}, "
        f"the raw table's was {raw_max:.3f})")
