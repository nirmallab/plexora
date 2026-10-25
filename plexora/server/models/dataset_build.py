"""From a dataset plan to registered projects, through the one creation service.

The plan (`dataset_plan`) says which files make which project; this turns it
into project specs -- the `PROJECT_SPEC_KEYS` vocabulary the Python API and
`plexora dataset create --from` read -- and registers them with
`plexora.create_dataset`, so a dataset made from the dialog is made exactly as
one made from a script. Files are referenced where they are; nothing is copied.

A loose project is `{image, name, segmentation?, data?, subset?,
channel_names?}`. A run or store (Xenium, Visium, SpatialData, DICOM) is
`{paths: [root, mask?, table?], name}`, registered by detection as Import
Sample registers it, because its parts are not files `create_project` can be
told about one by one.

Where the files are decides the rest. On a data node a spec names the node
(`node`), and the node reads its own files as `create_project` already
arranges. On a files-only server (`sftp://`) every project is a `paths` spec
with each file's role stated in `answers` (`added-as:`), because that is the
route that streams an image and a mask from such a server and copies its CSV
table across (`replace_project_data`).

Creation is a background task (`layer_jobs`, pseudo-sample `dataset-build`):
every path is checked first, then each project is registered in turn, and one
that fails is reported and skipped rather than stopping the rest --
`create_dataset` raises on the first failure and keeps what landed, so the
task resumes it with the specs that remain.
"""

from __future__ import annotations

import threading

TASK_OWNER = "dataset-build"

BUILD_STAGES = {
    "checking": (0, 5, "Checking files"),
    "registering": (5, 97, "Registering projects"),
}

#: token -> the build's record: `{name, projects: [...], dataset, error}`.
_BUILDS: dict = {}
_LOCK = threading.Lock()


def spec_for(project, place=None) -> dict | None:
    """One plan project as a project spec, or None when it cannot be made.

    @param place - the plan's `place`: None on this computer.
    """
    if project.get("status") == "unavailable":
        return None
    kind = (place or {}).get("kind")
    image = project["image"]
    extras = [project.get(slot) for slot in ("mask", "table")]
    if project["kind"] == "bundle":
        paths = [image["path"]] + [slot["path"] for slot in extras
                                   if slot and slot.get("path") and not slot.get("bundled")]
        spec = {"paths": paths, "name": project["name"]}
        if kind == "node":
            spec["node"] = place["name"]
        if project.get("existing"):
            spec["exist_ok"] = True
        return spec
    if kind == "sftp":
        return _sftp_spec(project)
    spec = {"image": image["path"], "name": project["name"]}
    mask, table = extras
    if mask and mask.get("path"):
        spec["segmentation"] = mask["path"]
    if table and table.get("path"):
        spec["data"] = table["path"]
        if table.get("subset") and table["subset"].get("value") is not None:
            spec["subset"] = {"column": table["subset"]["column"],
                              "value": table["subset"]["value"]}
    channels = project.get("channels") or {}
    if channels.get("names"):
        spec["channel_names"] = list(channels["names"])
    if kind == "node":
        spec["node"] = place["name"]
    if project.get("existing"):
        spec["exist_ok"] = True
    return spec


def _sftp_spec(project) -> dict:
    """A project on a files-only server, as the files Import Sample is given.

    Each file's role is said (`added-as:`), so a whole-cell mask is never read
    as a second image and a table never has to be guessed at. The channel
    list goes in as a file: Import Sample names the image's channels from it.
    """
    paths = [project["image"]["path"]]
    answers = {}
    for slot, role in (("mask", "mask"), ("table", "table")):
        found = project.get(slot)
        if found and found.get("path"):
            paths.append(found["path"])
            answers[f"added-as:{found['path']}"] = role
    channels = project.get("channels") or {}
    if channels.get("names") and channels.get("path"):
        paths.append(channels["path"])
    spec = {"paths": paths, "name": project["name"]}
    if answers:
        spec["answers"] = answers
    if project.get("existing"):
        spec["exist_ok"] = True
    return spec


def specs_for(plan, only=None) -> list:
    """`[(project id, spec)]` for every project the plan can make.

    @param only - project ids to keep; everything else is left out.
    """
    wanted = set(only) if only else None
    found = []
    for project in plan["projects"]:
        if wanted is not None and project["id"] not in wanted:
            continue
        spec = spec_for(project, plan.get("place"))
        if spec is not None:
            found.append((project["id"], spec))
    return found


def spec_document(plan, name, description="") -> dict:
    """The file `plexora dataset create NAME --from FILE` reads, for this plan."""
    return {"name": name, "description": description or "",
            "projects": [spec for _id, spec in specs_for(plan)]}


def build(plan, *, name, description="", exist_ok=False, only=None,
          on_progress=None, cancel=None, report_to=None) -> dict:
    """Register the plan's projects into dataset `name`. Synchronous.

    @param on_progress - `on_progress(done, total, project_name)`.
    @param report_to - a dict whose `projects` is kept current as each
        project starts and settles, for a poller to read.
    @returns `{dataset, projects: [{id, name, status, error, warnings}]}`,
        `status` being `created`, `adopted`, `failed` or `skipped`.
    """
    from plexora import datasets as api
    from plexora.server.models import datasets as registry

    by_id = {project["id"]: project for project in plan["projects"]}
    pairs = specs_for(plan, only)
    results = {}
    for project in plan["projects"]:
        if only and project["id"] not in only:
            continue
        record = {"id": project["id"], "name": project["name"], "status": "pending",
                  "error": None, "warnings": list(project.get("warnings") or ())}
        if project.get("status") == "unavailable":
            record.update(status="failed",
                          error=f"{project['image']['name']} is no longer there.")
        elif project.get("existing") and project.get("table") and project["kind"] != "bundle":
            record["warnings"].append(
                "Already imported: its mask is attached, its table is left as it was.")
        results[project["id"]] = record

    def publish():
        if report_to is not None:
            report_to["projects"] = [dict(r) for r in results.values()]

    ids = [project_id for project_id, _spec in pairs]
    total = len(ids)
    remaining = [spec for _pid, spec in pairs]
    dataset, first, offset = None, True, 0

    def progress(index, _count, spec):
        if spec is None:
            return
        position = offset + index
        results[ids[position]]["status"] = "registering"
        publish()
        if on_progress is not None:
            on_progress(position, total, results[ids[position]]["name"])

    publish()
    while remaining and not (cancel is not None and cancel.is_set()):
        try:
            dataset = api.create_dataset(
                name, projects=remaining, description=description if first else "",
                exist_ok=exist_ok or not first, on_progress=progress, cancel=cancel)
            for position in range(offset, offset + len(remaining)):
                if results[ids[position]]["status"] == "registering":
                    _settle(results[ids[position]], by_id)
            remaining = []
        except api.DatasetCreateError as error:
            dataset = error.dataset or dataset
            # Registration is in order and stops at the first failure, so the
            # failed entry is the one after everything that was created.
            failed_at = len(error.created)
            for position in range(offset, offset + failed_at):
                _settle(results[ids[position]], by_id)
            record = results[ids[offset + failed_at]]
            record["status"] = "failed"
            record["error"] = str(error.cause or error)
            remaining = remaining[failed_at + 1:]
            offset += failed_at + 1
        first = False
        publish()
    for record in results.values():
        if record["status"] in ("pending", "registering"):
            record["status"] = "skipped"
    publish()
    if dataset is None:
        found = registry.find_by_name(name)
        if found is not None:
            dataset = api.Dataset._from_record(found)
    if on_progress is not None:
        on_progress(total, total, None)
    return {"dataset": dataset, "projects": list(results.values())}


def _settle(record, by_id):
    project = by_id[record["id"]]
    record["status"] = "adopted" if project.get("existing") else "created"


def check_name(name, *, exist_ok=False) -> None:
    """Refuse a dataset name that is empty, or taken without `exist_ok`."""
    from plexora.server.models import datasets as registry

    if not str(name or "").strip():
        raise ValueError("Name the dataset.")
    found = registry.find_by_name(str(name).strip())
    if found is not None and not exist_ok:
        raise FileExistsError(f"There is already a dataset called {found.name}.")


def start_build(token, plan, *, name, description="", exist_ok=False,
                only=None) -> dict:
    """Run `build` as a background task filed under `token`; its first status."""
    from plexora.server.models import dataset_plan, layer_jobs

    check_name(name, exist_ok=exist_ok)
    plan = dataset_plan.verify(plan)
    state = {"name": str(name).strip(), "projects": [], "dataset": None,
             "error": None, "label": None}
    with _LOCK:
        _BUILDS[token] = state

    def work(stage, report, cancel):
        stage("checking")
        stage("registering")

        def progress(done, total, project_name):
            state["label"] = (f"Registering {done + 1} of {total}: {project_name}"
                              if project_name else None)
            report(done, total)

        try:
            result = build(plan, name=state["name"], description=description,
                           exist_ok=exist_ok, only=only, cancel=cancel,
                           on_progress=progress, report_to=state)
        except Exception as error:
            state["error"] = str(error)
            raise
        state["dataset"] = result["dataset"]

    layer_jobs.start_task(TASK_OWNER, token, work, stages=BUILD_STAGES)
    return build_status(token)


def build_status(token) -> dict:
    """`{status, progress, stage_label, projects, dataset, error}`."""
    from plexora.server.models import layer_jobs

    with _LOCK:
        state = _BUILDS.get(token)
    if state is None:
        raise KeyError(token)
    record = layer_jobs.tasks(TASK_OWNER, prefix=token).get(token) or {}
    dataset = state.get("dataset")
    projects = state.get("projects") or []
    return {
        "name": state["name"],
        "status": record.get("status", "pending"),
        "progress": record.get("progress", 0),
        "stage_label": state.get("label") or record.get("stage_label"),
        "error": state.get("error") or record.get("error"),
        "projects": projects,
        "counts": {
            "created": sum(1 for p in projects if p["status"] == "created"),
            "adopted": sum(1 for p in projects if p["status"] == "adopted"),
            "failed": sum(1 for p in projects if p["status"] == "failed"),
            "skipped": sum(1 for p in projects if p["status"] == "skipped"),
        },
        "dataset": ({"id": dataset.id, "name": dataset.name,
                     "projects": list(dataset)} if dataset is not None else None),
    }


def cancel_build(token) -> bool:
    from plexora.server.models import layer_jobs

    return layer_jobs.stop_task(TASK_OWNER, token, timeout=1.0) or True


def forget():
    with _LOCK:
        _BUILDS.clear()

