"""Stored pickles are read back admitting only what Plexora writes into them.

The cell index cache can live in a lab's shared root and the gate and channel
rows in a project database there; a plain pickle.load of a file someone else
can replace runs whatever it asks for. The format is unchanged -- these pin
that every shape Plexora actually stores still reads, and that nothing else
does.
"""

import datetime
import os
import pickle
import subprocess
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from scipy.spatial import cKDTree

from plexora.server.utils import safe_pickle


def test_the_cell_index_state_still_reads():
    points = np.random.default_rng(0).random((500, 2)) * 1000
    state = {"version": 1, "tree": cKDTree(points), "rows": np.arange(500),
             "xs": points[:, 0], "ys": points[:, 1], "frames": None,
             "z0": np.zeros(500, dtype=np.float32), "z1": None,
             "reach": 2.5, "spacing": 1.0, "extent_from": "x"}
    back = safe_pickle.loads(pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL))
    assert back["tree"].query([10, 10])[1] == state["tree"].query([10, 10])[1]
    assert back["z0"].dtype == np.float32


def test_gate_and_channel_rows_still_read():
    rows = pl.DataFrame({"channel": ["CD4"], "gate_start": [1.5], "gate_end": [None],
                         "gate_active": [True], "at": [datetime.datetime(2026, 1, 2)]}).to_dicts()
    rows[0]["np_value"] = np.float64(3.25)
    assert safe_pickle.loads(pickle.dumps(rows, protocol=4)) == rows


class _Planted:
    def __reduce__(self):
        return (os.system, ("echo planted",))


def test_a_pickle_that_imports_anything_else_is_refused():
    with pytest.raises(safe_pickle.UnsafePickle):
        safe_pickle.loads(pickle.dumps(_Planted()))


def test_a_planted_index_cache_is_rebuilt_not_run(tmp_path, monkeypatch):
    from plexora.server.models import data_model

    (tmp_path / "manifest.json").write_text('{"rows": 1}', encoding="utf-8")
    (tmp_path / "index.pickle").write_bytes(pickle.dumps(_Planted()))
    ran = []
    monkeypatch.setattr(os, "system", lambda *a: ran.append(a))
    assert data_model._read_cached_index(tmp_path, {"rows": 1}) is None
    assert ran == []


def test_no_stored_pickle_is_read_with_plain_pickle():
    """Static: every load of stored state goes through safe_pickle."""
    root = Path(__file__).resolve().parents[1] / "plexora"
    found = subprocess.run(
        ["grep", "-rnE", r"pickle\.loads?\(", str(root), "--include=*.py"],
        capture_output=True, text=True).stdout.splitlines()
    offenders = [line for line in found
                 if "safe_pickle" not in line and "/tests/" not in line]
    assert offenders == []
