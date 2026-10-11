"""The 3-D segmentation card's routes: what it can use, and a run in the background.

`GET  /plugins/segmentation/api/state?datasource=` -> Cellpose's status here,
     the image's channels and spacing, the running task and the last run.
`POST /plugins/segmentation/api/run` -> 202 and the task record (layer_jobs);
     400 with the reason when it cannot run.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from plexora import api

segmentation_bp = Blueprint(
    "segmentation", __name__,
    template_folder="../templates",
    static_folder="../static",
    static_url_path="/static",
)

TASK = "segment3d"
STAGES = {"segmenting": (0, 90, "Segmenting cells in 3-D"),
          "measuring": (90, 99, "Measuring the cells")}


def _project(name):
    from plexora.server.models.project import Project

    if not name:
        raise KeyError(name)
    api.project_data(name)                     # KeyError for an unknown project
    return Project.load(name)


@segmentation_bp.route('/api/state', methods=['GET'])
def get_state():
    from plexora.plugins.segmentation.server import cellpose3d, run
    from plexora.server.models import layer_jobs

    try:
        project = _project(request.args.get('datasource'))
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400
    volume = api.project_data(project.name).image.volume() or {}
    spacing = volume.get("spacing") or {}
    return jsonify(
        success=True, cellpose=cellpose3d.status(),
        channels=list(project.image.channel_names),
        depth=volume.get("depth"), frames=volume.get("frames"),
        z_step_known=bool(volume.get("volumetric_ok")),
        spacing={axis: (spacing.get(axis) or {}).get("value") for axis in ("x", "z")},
        has_mask=bool(project.segmentation.available),
        task=layer_jobs.tasks(project.name).get(TASK),
        last=run.last_run(project))


@segmentation_bp.route('/api/run', methods=['POST'])
def post_run():
    from plexora.plugins.segmentation.server import cellpose3d, run
    from plexora.server.models import layer_jobs
    from plexora.server.models.project import Project

    body = request.get_json(silent=True) or {}
    try:
        project = _project(body.get('datasource'))
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400
    if not cellpose3d.status()["installed"]:
        state = cellpose3d.status()
        return jsonify(success=False, error=state.get("problem")
                       or f"Segmenting in 3-D needs Cellpose: {cellpose3d.INSTALL}",
                       install=cellpose3d.INSTALL), 400
    if not body.get('nuclear'):
        return jsonify(success=False, error="choose the nuclear channel"), 400
    options = {key: body.get(key) for key in (
        "nuclear", "cytoplasm", "mode", "diameter_um", "stitch_threshold",
        "cellprob_threshold", "min_size_um3", "level", "t", "device", "replace_mask")
        if body.get(key) not in (None, "")}
    if project.segmentation.available and not options.get("replace_mask"):
        return jsonify(success=False, needs="replace_mask",
                       error="This project already has a mask. Replace it? Its file is left "
                             "where it is."), 409
    name = project.name

    def work(stage, report, cancel):
        stage("segmenting")
        run.segment_project(Project.load(name), progress=report, cancelled=cancel.is_set,
                            stage=stage, **options)

    record = layer_jobs.start_task(name, TASK, work, stages=STAGES)
    return jsonify(success=True, task=record), 202
