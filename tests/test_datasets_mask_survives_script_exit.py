"""A mask given to `create_project` is drawn even after the script that gave it ends.

`plexora/datasets.py` promises that registration is synchronous: the call
blocks, and what it returns is the project. A script, a notebook cell followed
by a kernel restart, and `plexora project create` all end the process the
moment the call returns. The mask's conversion must therefore either finish
before `create_project` returns, or be picked up again the next time the
project is opened. A conversion left on a daemon thread that dies with the
process, and that nothing restarts, leaves the project's mask "pending" for
good: the viewer says it is converting, and it never arrives.

Each step runs in a process of its own, because the in-process suite joins
every `segmentation-*` thread at teardown (conftest `_finish_layer_builds`),
which is exactly the wait a real script never gets.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import tifffile

ROOT = Path(__file__).resolve().parents[1]

#: A mask the size the suite uses converts in milliseconds, which can beat the
#: interpreter's own shutdown. A real one takes tens of seconds; this delay
#: stands in for that, and nothing is asserted about time.
_SLOW_MASK = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from plexora import paths
    paths.settings_path = lambda: Path(sys.argv[1]) / ".plexora-settings.json"
    paths.reset()
    from plexora.server.models import data_model
    _real = data_model.convertOmeTiff
    def _slow(*args, **kwargs):
        if kwargs.get("isLabelImg"):
            time.sleep(3)
        return _real(*args, **kwargs)
    data_model.convertOmeTiff = _slow
""")

_CREATE = _SLOW_MASK + textwrap.dedent("""
    import json
    import plexora
    spec = json.loads(sys.argv[2])
    print("registered", plexora.create_project(**spec))
    # The script ends here, as `plexora project create` and a one-shot script do.
""")

_OPEN = textwrap.dedent("""
    import sys, threading
    from pathlib import Path
    from plexora import paths
    paths.settings_path = lambda: Path(sys.argv[1]) / ".plexora-settings.json"
    paths.reset()
    from plexora.server.models import data_model
    data_model.load_config(sys.argv[2])
    for thread in threading.enumerate():
        if thread.name.startswith("segmentation-"):
            thread.join(120)
    print("opened")
""")


def _run(script, *argv):
    done = subprocess.run([sys.executable, "-c", script, *argv], cwd=ROOT,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    return done


def _entry(root, name):
    return json.loads((root / "config.json").read_text(encoding="utf-8"))[name]


def _files(folder):
    image = folder / "slide.ome.tif"
    tifffile.imwrite(image, np.zeros((2, 256, 256), dtype=np.uint8))
    mask = folder / "mask.tif"
    labels = np.zeros((256, 256), dtype=np.uint32)
    for i in range(1, 5):
        labels[i * 40:i * 40 + 10, i * 40:i * 40 + 10] = i
    tifffile.imwrite(mask, labels)
    table = folder / "cells.csv"
    pl.DataFrame({
        "CellID": np.arange(1, 5, dtype=np.uint32),
        "X_centroid": [45.0, 85.0, 125.0, 165.0],
        "Y_centroid": [45.0, 85.0, 125.0, 165.0],
        "CD3": [0.0, 1.0, 2.0, 3.0],
    }).write_csv(table)
    return image, mask, table


@pytest.mark.parametrize("with_table", [True, False], ids=["with-table", "image-and-mask"])
def test_a_mask_given_to_create_project_is_ready_after_the_script_exits(
        tmp_path, with_table):
    """The script that called `create_project(..., segmentation=mask)` has
    exited. Either the mask was converted before the call returned, or opening
    the project afterwards converts it -- it is never left pending for good."""
    files = tmp_path / "files"
    files.mkdir()
    image, mask, table = _files(files)
    name = "maskafterexit_table" if with_table else "maskafterexit_image"
    spec = {"image": str(image), "segmentation": str(mask), "name": name}
    if with_table:
        spec.update(data=str(table), cell_id="CellID",
                    x="X_centroid", y="Y_centroid")

    _run(_CREATE, str(tmp_path), json.dumps(spec))
    after_create = _entry(tmp_path, name)

    if after_create.get("segmentation_status") != "ready":
        # Not finished by the time the script ended: opening the project in a
        # fresh process has to start the conversion again.
        _run(_OPEN, str(tmp_path), name)

    entry = _entry(tmp_path, name)
    assert entry.get("segmentation_status") == "ready", (
        f"after create_project's process exited: "
        f"segmentation={after_create.get('segmentation')!r} "
        f"status={after_create.get('segmentation_status')!r}; "
        f"after opening it again: segmentation={entry.get('segmentation')!r} "
        f"status={entry.get('segmentation_status')!r}")
    assert entry.get("segmentation")
