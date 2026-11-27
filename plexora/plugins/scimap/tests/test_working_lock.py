"""The page's poll must never open the working file while a step writes it.

HDF5 refuses to open a file for writing while this process holds a read handle
on it ("file is already open for read-only"). A step writes its result back at
the end of its run while the page polls the notebook every second, and each
poll describes the working file -- so the poll answers from its cache whenever
a run holds the file, and the readers on request threads wait their turn.
"""

from __future__ import annotations

import os
import threading
import time

import numpy as np
import pandas as pd
import pytest

ad = pytest.importorskip("anndata")

from plexora.plugins.scimap.server import working  # noqa: E402


def _file(path):
    adata = ad.AnnData(np.ones((4, 2), dtype="float32"),
                       obs=pd.DataFrame({"imageid": ["a", "a", "b", "b"]}, index=list("wxyz")))
    adata.write_h5ad(path)


def test_describe_answers_from_its_cache_while_a_step_writes(tmp_path):
    path = tmp_path / "work.h5ad"
    _file(path)
    first = working.describe(path)
    assert first["n_cells"] == 4

    holding, release = threading.Event(), threading.Event()

    def step():
        with working.writing(path):
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=step)
    thread.start()
    holding.wait(5)
    try:
        later = time.time() + 5
        os.utime(path, (later, later))  # the file changed under the run
        started = time.monotonic()
        assert working.describe(path) is first  # no handle opened, no wait
        assert time.monotonic() - started < 0.5
        with pytest.raises(TimeoutError):
            with working.reading(path, timeout=0.05):
                pass
    finally:
        release.set()
        thread.join(5)

    assert working.describe(path)["mtime"] != first["mtime"]
    with working.reading(path, timeout=0.5):
        pass
