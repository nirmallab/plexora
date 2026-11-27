"""Projects and datasets as records: read and edit them without opening one.

A plugin page that manages a cohort -- the SCIMAP notebook's project list --
needs to say what a project's table is, correct a column role, group projects
into a dataset and notice when another tab did any of that, all without
loading a datasource (which loads the image and the table into this process).
These are thin: every write goes through the function the core surfaces
already use, so a role set here means what it means on the edit page.

- `project_info` / `update_project` -- one project's cell-table facts. The
  edit applies the column answers through `plexora.datasets._apply_columns`
  (the requirements modal's and the edit page's `tool_routes._apply`, with the
  file checked against the answer and put back when it will not read) and a
  new table through `plexora.datasets.configure_project`, then tells the open
  viewers (`core`/`dataset.changed`).
- `list_datasets` / `create_dataset` / `update_dataset` / `delete_dataset` --
  the folder a cohort lives in (`server/models/datasets.py`).
- `records_stamp` -- a string that moves whenever any of the above changes
  on disk, for a page to poll.
"""

from __future__ import annotations

import hashlib
import os

#: Microns per one of each unit a calibration may be stated in.
_MICRONS_PER = {"µm": 1.0, "μm": 1.0, "um": 1.0, "micron": 1.0, "microns": 1.0,
                "nm": 1e-3, "mm": 1e3}


def _project(name):
    from plexora.server.models.project import Project

    project = Project.find(name)
    if project is None:
        raise KeyError(f"no project named {name!r}")
    return project


def _microns(pixel_size):
    """A `{value, unit}` calibration in µm per pixel, or None."""
    if not pixel_size:
        return None
    factor = _MICRONS_PER.get(str(pixel_size.get("unit") or "µm").strip())
    if factor is None:
        return None
    return float(pixel_size["value"]) * factor


def _roles(project) -> dict:
    """Every column role, in the file's own vocabulary -- what `update_project`
    takes back. For AnnData/SpatialData the cell id is the obs field and x/y
    are obs columns (None when the coordinates come from an obsm array, which
    `coordinates` then names); for a flat table they are its columns."""
    from plexora.server.models.project import ROLE_NAMES

    out = {role: project.roles.get(role) for role in ROLE_NAMES}
    if project.dataset is not None and project.columns_are_structural:
        coordinates = project.dataset.coordinates or {}
        obs = coordinates.get("source") == "obs"
        out.update(cell_id=project.dataset.obs_id_field,
                   x=coordinates.get("x_column") if obs else None,
                   y=coordinates.get("y_column") if obs else None)
    return out


def list_projects() -> list:
    """Every project's name, sorted, read from the records alone.

    Returns:
        list: Project names (str).
    """
    from plexora.server.models.project import Project

    return sorted(Project.load_all())


def project_info(name) -> dict:
    """One project's cell-table facts, read from its record alone.

    Nothing is loaded: no image is opened and no table is read, so this is
    cheap enough to call for every project in a list.

    Args:
        name (str): The project's name.

    Returns:
        dict: `name`, `has_table` (a table that can be read: a file whose table
        has not been chosen yet is False), `type` (`"csv"`, `"anndata"`, ...),
        `src` (the table's path) and `table` (the table inside a multi-table
        store), all None without a table; `roles` (every column role --
        `cell_id`, `x`, `y`, `image_id`, `celltype`, `z`, `t` -- None where
        unset, in the file's own vocabulary); `coordinates` (AnnData and
        SpatialData: where positions are read from, `{}` otherwise);
        `markers`; `metadata_columns` (the obs columns of an AnnData, the
        non-marker columns of a flat table); `pixel_size` (µm per pixel when a
        person recorded one, else None -- a calibration the image file states
        is read from the file and is not in the record); `image` (`{src,
        channels}`, None for a sample with no image); `mask` (`{src, status}`,
        None without one); `log_transformed`; `subset` (the table's row filter,
        `{column, value}`, `{}` without one); `on_node` (the table is held by a
        data node); `pixel_size_effective` (µm per pixel as the scale bar uses
        it: the recorded calibration, else the image file's own, else None);
        `last_opened_at` (ISO time the viewer last opened it, or None).

    Raises:
        KeyError: If no project is registered under `name`.
    """
    project = _project(name)
    spec = project.dataset
    structural = spec is not None and project.columns_are_structural
    image = project.image
    seg = project.segmentation
    return {
        "name": project.name,
        "has_table": project.has_table,
        "type": spec.type if spec else None,
        "src": spec.src if spec else None,
        "table": spec.table if spec else None,
        "roles": _roles(project),
        "coordinates": dict(spec.coordinates or {}) if structural else {},
        "markers": list(project.columns.markers),
        "metadata_columns": (list(spec.obs_columns) if structural and spec.obs_columns
                             else list(project.columns.metadata)),
        "pixel_size": _microns(image.pixel_size),
        "image": (None if image.is_blank
                  else {"src": image.src, "channels": image.channel_names}),
        "mask": ({"src": seg.source or seg.derived, "status": seg.status}
                 if seg.requested else None),
        "log_transformed": project.log_transformed,
        "subset": dict(spec.subset or {}) if spec else {},
        "on_node": _on_node(project),
        "pixel_size_effective": _effective_microns(project),
        "last_opened_at": getattr(project, "last_opened_at", None),
    }


def _on_node(project) -> bool:
    binding = getattr(project, "resources", {}).get("table") if hasattr(project, "resources") else None
    return bool(binding is not None and getattr(binding, "is_node", False))


def _effective_microns(project):
    """µm per pixel as the viewer's scale bar uses it: a recorded calibration,
    else the image file's own. None when neither says."""
    try:
        from plexora.server.utils import pixel_scale

        size = pixel_scale.pixel_size(project)
        return size["value"] if size else None
    except Exception:  # noqa: BLE001 -- uncalibrated is an honest answer
        return None


def update_project(name, *, roles=None, markers=None, metadata=None,
                   pixel_size=None, table_src=None) -> dict:
    """Edit a project's cell-table answers, as the edit page saves them.

    Each argument left None is left as it is. The answers are recorded as
    confirmed (nothing asks for them again), and the file is checked against
    them: an AnnData that cannot be read with a new cell id is put back and
    the call raises. Open viewers of the project are told its dataset changed
    (`core`/`dataset.changed`), and this server's copy is re-read if it has
    that project loaded.

    Args:
        name (str): The project's name.
        roles (dict, optional): Role -> column, in `project_info`'s `roles`
            vocabulary. `""` or None clears a role; a role not named is kept.
        markers (list, optional): The marker columns. Given alone, every
            other column the record knows becomes metadata.
        metadata (list, optional): The metadata columns. Given alone, every
            other column becomes a marker.
        pixel_size (float, optional): Microns per pixel; 0 clears it.
        table_src (str, optional): A new cell-table file (a path, or a
            `node://` address), read as `plexora.configure_project(data=...)`
            reads it. Applied before the roles, which then name its columns.

    Returns:
        dict: `project_info(name)` after the edit.

    Raises:
        KeyError: If no project is registered under `name`.
        ValueError: If an answer cannot be applied or the file cannot be read
            with it. Nothing of the edit is kept.
    """
    from plexora import datasets as scripting
    from plexora.api import notify_viewers

    before = _project(name)
    sections = []
    try:
        if table_src is not None:
            scripting.configure_project(name, data=table_src)
            sections.append("file")
        payload = {}
        if roles:
            payload["roles"] = {role: (column or "") for role, column in roles.items()}
        if markers is not None or metadata is not None:
            payload["columns"] = {"markers": list(markers or ()),
                                  "metadata": list(metadata or ())}
        if payload:
            scripting._apply_columns(name, payload)
            # Which columns are markers, and which is the id or a position,
            # change what every value of the table is read as: a viewer
            # re-reads the whole thing (agentBridge.js), as after the edit page.
            sections.append("var" if "roles" not in payload else "file")
        if pixel_size is not None:
            from plexora.datasource import set_pixel_size

            set_pixel_size(name, float(pixel_size) or None)
            # The scale bar is drawn from the loaded image's metadata, so a
            # tab reloads for it, as `/set_pixel_size` reloads the datasource.
            sections.append("file")
    except Exception:
        from plexora.server.models.project import Project

        if Project.find(name) != before:
            before.save()
        raise
    if sections:
        notify_viewers(name, "core", "dataset.changed",
                       {"sections": list(dict.fromkeys(sections)),
                        "by": "update_project"})
    return project_info(name)


# -- datasets --------------------------------------------------------------


def _known() -> set:
    from plexora.server.models.project import Project

    return set(Project.load_all())


def list_datasets() -> list:
    """Every dataset, by name.

    Returns:
        list: One dict per dataset, `{id, name, description, createdAt,
        meta, projects, projectCount}` -- the same record `GET /datasets`
        lists. `projects` holds the member projects that exist now.
    """
    from plexora.server.models import datasets

    return [dataset.to_wire()
            for dataset in sorted(datasets.load_all(known=_known()).values(),
                                  key=lambda d: d.name.casefold())]


def create_dataset(name, projects=()) -> dict:
    """Make a dataset. Each project is taken out of any dataset it was in.

    Args:
        name (str): Its name; unique, ignoring case.
        projects (list, optional): Names of projects to put in it.

    Returns:
        dict: The new dataset, as `list_datasets` lists one.

    Raises:
        ValueError: If the name is empty or taken, or a project does not exist.
    """
    from plexora.server.models import datasets

    return datasets.create(name, projects=projects or (), known=_known()).to_wire()


def update_dataset(dataset_id, *, name=None, projects=None, meta=None) -> dict:
    """Rename a dataset, set its members, or edit its `meta`.

    Args:
        dataset_id (str): The dataset's id.
        name (str, optional): A new name.
        projects (list, optional): Exactly the projects it holds afterwards.
            A project added is taken out of any other dataset; one dropped is
            in none.
        meta (dict, optional): Merged into its `meta` key by key; a key set to
            None is removed.

    Returns:
        dict: The dataset after the edit, as `list_datasets` lists one.

    Raises:
        ValueError: If there is no such dataset, the name is taken, or a
            project does not exist.
    """
    from plexora.server.models import datasets

    known = _known()
    dataset = datasets.get(dataset_id, known=known)
    if name is not None:
        dataset = datasets.rename(dataset_id, name)
    if projects is not None:
        wanted = datasets._check_projects(projects, known)
        dropped = [member for member in dataset.projects if member not in wanted]
        if dropped:
            datasets.assign(dropped, None, known=known)
        added = [member for member in wanted if member not in dataset.projects]
        if added:
            datasets.assign(added, dataset_id, known=known)
    if meta is not None:
        datasets.describe(dataset_id, meta=dict(meta))
    return datasets.get(dataset_id, known=known).to_wire()


def delete_dataset(dataset_id) -> dict:
    """Delete a dataset. Its projects are not touched; they are in none.

    Args:
        dataset_id (str): The dataset's id.

    Returns:
        dict: `{id, released}`, the projects it held.

    Raises:
        ValueError: If there is no such dataset.
    """
    from plexora.server.models import datasets

    dataset = datasets.remove(dataset_id)
    if dataset is None:
        raise datasets.DatasetError(f"no dataset with id {dataset_id!r}")
    return {"id": dataset.id, "released": list(dataset.projects)}


def records_stamp() -> str:
    """A short string that changes whenever a project record or a dataset does.

    Made from the modification time and size of every root's `config.json`
    and of the user's `datasets.json` files: no file is read. A page polls it
    and re-lists only when it moves. It moves for writes that change nothing
    a page shows as well -- opening a project stamps when it was last opened.

    Returns:
        str: 16 hex characters.
    """
    from plexora import paths
    from plexora.server.models import datasets

    files = [paths.config_path(root) for root in paths.roots()]
    files += [datasets.datasets_path(root) for root in paths.owned_roots()]
    parts = []
    for path in dict.fromkeys(files):
        try:
            stat = os.stat(path)
            parts.append(f"{path}\0{stat.st_mtime_ns}\0{stat.st_size}")
        except OSError:
            parts.append(f"{path}\0-")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]
