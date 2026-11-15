"""Two measurements of one project at once run one after the other (io-volume-6).

The Depth card and the agent can both start one. They write the same table,
the same `.tmp` names and the same plane-index temp folder: run together, the
second deleted the first's temp folder and the published index could mix the
two passes.
"""

import threading
import time
from types import SimpleNamespace

import polars as pl

from plexora.server.volume import objects


def test_two_measurements_of_one_project_never_overlap(tmp_path, monkeypatch):
    active, overlapped = [0], []

    def measure_columns(labels, source, **kwargs):
        active[0] += 1
        overlapped.append(active[0] > 1)
        time.sleep(0.2)
        active[0] -= 1
        return pl.DataFrame({"id": [1, 2]}), {"units": "voxel"}

    monkeypatch.setattr(objects.volume_labels, "project_label_volume", lambda project: object())
    monkeypatch.setattr(objects, "measure_columns", measure_columns)
    monkeypatch.setattr(objects, "object_paths",
                        lambda project: (tmp_path / objects.TABLE_NAME, tmp_path / objects.MANIFEST_NAME))
    from plexora.server.utils import source_image

    monkeypatch.setattr(source_image, "open_project_volume",
                        lambda project: SimpleNamespace(close=lambda: None))
    project = SimpleNamespace(name="p", segmentation=SimpleNamespace(available=True, derived="m"),
                              image=SimpleNamespace(channel_names=["a"]), has_table=True,
                              resource=lambda kind: None)
    errors = []

    def run():
        try:
            objects.measure_project_objects(project, units="voxel", attach=False)
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert not errors
    assert overlapped == [False, False]
