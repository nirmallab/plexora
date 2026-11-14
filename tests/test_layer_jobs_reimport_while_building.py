"""A layer re-imported while its build is still running.

`layer_jobs.start` leaves a running build alone and hands back its record, so
two builds never race for one cache directory. But the running build read the
project once, when it started, and `_finish` stamps whatever layer carries that
id NOW as `ready`. A re-import that corrects the layer (a new source, a new
transform: `import_sample._register_sample(replace=...)`) while the first build
runs must not end with the corrected layer marked ready from the old snapshot:
either it is built again from what it now says, or it stays pending.
"""

import threading

import numpy as np
import tifffile

import plexora
from plexora import _work
from plexora.server.models import layer_jobs
from plexora.server.models.project import LayerSpec, Project

from tests.helpers import use_data_root

NAME = "reimport_while_building"


def _layer(tmp_path, src, transform):
    return LayerSpec(id="tx", kind="points", modality="transcripts",
                     label="Transcripts", src=str(tmp_path / src),
                     transform=transform, status="pending")


def _join_builds(timeout=10.0):
    for thread in threading.enumerate():
        if thread.name.startswith(f"layer-{NAME}-") and thread.is_alive():
            thread.join(timeout)


def test_a_layer_reimported_mid_build_is_not_stamped_ready_from_the_old_one(
        tmp_path, monkeypatch):
    """The user imports a run, its transcript build starts, and before it ends
    they re-import the sample with the pixel size corrected. The layer that
    ends up `ready` must have been built from the corrected spec."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    tifffile.imwrite(tmp_path / "slide.ome.tif",
                     np.full((1, 64, 64), 5, dtype=np.uint16))
    plexora.datasource.register_image_datasource(NAME, tmp_path / "slide.ome.tif")
    old_transform = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    new_transform = (4.7, 0.0, 0.0, 0.0, 4.7, 0.0)
    Project.mutate(NAME, lambda p: p.with_layer(
        _layer(tmp_path, "old.parquet", old_transform)))
    layer_jobs.forget(NAME)
    saved_builders = dict(layer_jobs._BUILDERS)

    started = threading.Event()
    release = threading.Event()
    built = []

    def builder(project, layer, stage, report):
        built.append((layer.src, tuple(layer.transform or ())))
        if len(built) == 1:
            started.set()
            release.wait(10)

    try:
        layer_jobs.register_builder("transcripts", builder)

        # The first import starts the build, which is now running.
        project = Project.load(NAME)
        assert layer_jobs.start_builder(project, project.layer("tx")) is True
        assert started.wait(10), "the first build never started"

        # The re-import: the layer is rewritten in place, pending again, and
        # its builder is asked for -- exactly what `_register` does.
        Project.mutate(NAME, lambda p: p.with_layer(
            _layer(tmp_path, "new.parquet", new_transform)))
        project = Project.load(NAME)
        assert project.layer("tx").pending
        layer_jobs.start_builder(project, project.layer("tx"))

        # The first build ends.
        release.set()
        _join_builds()
        _work.run_pending()
        _join_builds()

        final = Project.load(NAME).layer("tx")
        built_new = (str(tmp_path / "new.parquet"), new_transform) in built
        assert final.status != "ready" or built_new, (
            f"the re-imported layer was stamped {final.status!r} but only "
            f"these specs were ever built: {built}")
    finally:
        release.set()
        _join_builds()
        layer_jobs.forget(NAME)
        layer_jobs._BUILDERS.clear()
        layer_jobs._BUILDERS.update(saved_builders)
