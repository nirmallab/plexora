"""Two notebooks open at once, on one data root, each serving its own objects.

JupyterLab and VS Code both run a kernel per notebook, and `plexora/jupyter.py`
already shares one sidecar between kernels on the same data root. So two kernel
nodes alive at the same time is an ordinary state, not a restart: the second
kernel must not take over the first kernel's projects, and a notebook that
passes an array with no name must not be handed a project another live kernel
is serving.

Both kernels live in this one process here: each `KernelNode` is a real node on
its own loopback port with its own token and registry, and swapping
`KernelNode._instance` is exactly what "this call runs in the other kernel"
means to `register_memory_datasource`. The sidecar is `plexora.app`'s own
data_model, as in `tests/test_memory_datasource.py`.
"""

import numpy as np
import pandas as pd
import pytest

from plexora import memory as memory_api
from plexora.server.models import data_model

QUIET = {"log": lambda *args, **kwargs: None}


@pytest.fixture
def kernels(monkeypatch):
    """Two live kernel nodes, A and B, and a way to say which one is calling."""
    started = []

    def start():
        node = memory_api.KernelNode(**QUIET)
        started.append(node)
        return node

    def act_as(node):
        monkeypatch.setattr(memory_api.KernelNode, "_instance", node)

    monkeypatch.setattr(memory_api.KernelNode, "_instance", None)
    yield start, act_as
    for node in started:
        try:
            node._server.close()
        except Exception:  # noqa: BLE001 -- teardown only
            pass


def _image():
    return np.zeros((2, 256, 256), dtype=np.uint8)


def _table(cd3, rows):
    return pd.DataFrame({
        "X": np.linspace(10.0, 200.0, rows),
        "Y": np.linspace(10.0, 200.0, rows),
        "CD3": [float(cd3)] * rows,
    })


def _cd3_the_viewer_reads(name):
    """Every cell's CD3 value, as the sidecar's own gating path reads it."""
    data_model.load_datasource(name, reload=True, wait_for_table=True)
    columns = data_model.get_filter_columns(name, ["CD3"])
    assert "CD3" in columns, (
        f"the viewer has no CD3 for {name!r}: "
        f"{data_model.resource_unavailable(name, 'table')!r}")
    return sorted(np.asarray(columns["CD3"]).tolist())


def test_a_second_kernel_does_not_take_over_the_first_kernels_projects(
    kernels, plexora_data_root
):
    """Kernel A registers its project; kernel B starts and registers another.
    Kernel A's project must still read kernel A's cells -- B starting up must
    not repoint A's bindings at B's node, which does not hold them."""
    start, act_as = kernels

    kernel_a = start()
    act_as(kernel_a)
    memory_api.register_memory_datasource(
        "analysis_a", _image(), table=_table(cd3=1, rows=2), x="X", y="Y",
        data_dir=plexora_data_root, **QUIET)

    kernel_b = start()
    act_as(kernel_b)
    memory_api.register_memory_datasource(
        "analysis_b", _image(), table=_table(cd3=9, rows=3), x="X", y="Y",
        data_dir=plexora_data_root, **QUIET)

    assert _cd3_the_viewer_reads("analysis_a") == [1.0, 1.0]


def test_an_unnamed_array_in_a_second_kernel_gets_a_project_of_its_own(
    kernels, plexora_data_root
):
    """`plexora.view(image=array, table=...)` with no name, in two notebooks.

    The first kernel's project is called "notebook". The second kernel must not
    be handed that same name (it would overwrite the first notebook's project
    and the first viewer would draw the second notebook's cells), and after it
    registers, the first kernel's project must still read the first kernel's
    cells."""
    start, act_as = kernels

    kernel_a = start()
    act_as(kernel_a)
    name_a = memory_api.infer_project_name(_image(), plexora_data_root)
    memory_api.register_memory_datasource(
        name_a, _image(), table=_table(cd3=1, rows=2), x="X", y="Y",
        data_dir=plexora_data_root, **QUIET)

    kernel_b = start()
    act_as(kernel_b)
    name_b = memory_api.infer_project_name(_image(), plexora_data_root)
    assert name_b != name_a, (
        f"the second kernel was handed {name_b!r}, the project the first "
        f"kernel is serving")
    memory_api.register_memory_datasource(
        name_b, _image(), table=_table(cd3=9, rows=3), x="X", y="Y",
        data_dir=plexora_data_root, **QUIET)

    assert _cd3_the_viewer_reads(name_a) == [1.0, 1.0]


def test_a_second_kernel_cannot_silently_repoint_a_live_kernels_project(
    kernels, plexora_data_root
):
    """Two analyses of one slide, both named after it. The project belongs to
    the first kernel, which is still running: the second kernel registering
    the same name is the "project this kernel did not make" that
    `_refuse_foreign` exists to refuse -- and whatever the second call does,
    the first kernel's viewer must never draw the second kernel's cells."""
    start, act_as = kernels

    kernel_a = start()
    act_as(kernel_a)
    memory_api.register_memory_datasource(
        "slide_7", _image(), table=_table(cd3=1, rows=2), x="X", y="Y",
        data_dir=plexora_data_root, **QUIET)

    kernel_b = start()
    act_as(kernel_b)
    try:
        memory_api.register_memory_datasource(
            "slide_7", _image(), table=_table(cd3=9, rows=3), x="X", y="Y",
            data_dir=plexora_data_root, **QUIET)
    except memory_api.MemoryDataError:
        pass  # refused: the right answer

    assert _cd3_the_viewer_reads("slide_7") == [1.0, 1.0], (
        "the first kernel's project now reads the second kernel's cells")
