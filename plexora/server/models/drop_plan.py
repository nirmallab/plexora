"""What files dropped on the window are, relative to what is open, and what to
do with each.

The import engine (`import_proposal.inspect_paths`) already says what a file
IS -- an image, a label mask, a cell table, a channel list, a run folder. What
it cannot say is what the file is FOR at the moment it is dropped: a mask
dropped on an open slide is that slide's segmentation, the same mask dropped on
the home page is a question about which slide. This module is that second
step. It classifies nothing on its own; it composes the engine's answer with a
snapshot of the open project and turns each item into one action:

    inspect_drop(paths, datasource=..., target=...) -> plan

    plan = {
      "target": str|None,          # the zone the client asked about
      "datasource": str|None,      # the open project, when there is one
      "open": {name, title, width, height, channelCount, genericNames,
               hasMask, hasTable} | None,
      "items": [{pick, path, kind, label, detail, fit}],
          # kind: image | mask | table | channels | bundle | layer | folder
          #       | project_dir | <a plugin's kind>
      "actions": [{
          id, kind, status: "auto" | "needs_confirm" | "blocked",
          reason, message, picks, paths, sample,
          route: {method, path, json | form} | None,
          choices: [{value, label, kind, route, tone}] | None, default,
          one_press, effects: {reload, navigate}, then: [actions],
          fit,                      # the item's fit, for the confirm body
      }],
      "unrecognised": [{pick, path, reason, code}],
      "warnings": [str],
      "summary": {one_press, needs_dialog, hint, tone},
      "proposal": Proposal.to_dict() | None,   # for the import dialog
    }

`route` is literally what the client sends, to a route that already exists
(`/import/layers`, `/import/sample`, `/upload_channels`), so
an action the user confirmed and one that ran on its own take one path, and
the page's existing waits (the reload after a mask, the table's progress)
follow either. Nothing here writes.

The confidence policy is `decide`, a pure function of an item's fit and the
open project: a mask whose size differs from the image is never attached
without asking, whatever it is called; an existing mask is never replaced
without asking; a name alone never moves the user to another project.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from plexora.server.models import import_proposal as engine
from plexora.server.providers.base import is_remote_locator

#: Channel names a scanner or converter makes up: "Channel 1", `Channel_0`,
#: OME's `Channel:0:0`, "ch3". A project still carrying these has never been
#: given its panel, so a channel list dropped on it is applied without asking.
GENERIC_CHANNEL = re.compile(r"^(channel|ch|c)[\s_:-]*\d+(:\d+)*$", re.IGNORECASE)

#: What a segmentation's filename adds to the slide's: `slide_cellmask.tif`,
#: `slide-nuclei_labels.tif`. Stripped before the name is compared with the
#: open image's.
_MASK_SUFFIX = re.compile(
    r"([_\-. ]+(whole[_-]?)?(cell|cells|nuclei|nuclear|nucleus|membrane)?"
    r"[_\-. ]?(masks?|segmentation|segmentations|seg|labels?|labelled|labeled)"
    r"|[_\-. ]+(cell|nuclei|nuclear)?masks?|[_\-. ]+(cells|nuclei))$")

#: How many projects a mask dropped with nothing open is offered against.
TARGET_LIMIT = 8

#: A GeoJSON over this is not opened to count its features on hover.
SHAPES_COUNT_LIMIT = 50 << 20

#: Plugin-contributed file kinds (`register_sniffer`).
_SNIFFERS: list = []

#: `(paths, datasource, target, mtimes) -> (time, plan)`: hover asks once per
#: drag and the drop asks again, and the answer has not changed in between.
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_SECONDS = 30
_CACHE_SIZE = 16


def register_sniffer(fn) -> None:
    """Add a file kind core does not know about.

    `fn(path, context) -> dict | None`, asked of every local file before the
    import engine is, so a plugin's own sheet is not read as a cell table.
    `context` is `{"open": <snapshot or None>, "target": <zone name>}`. The
    dict is an item plus its action: `{"kind", "label", "detail"?, "action":
    {"kind", "status", "reason"?, "message", "route"?, "effects"?}}`. The
    action's `kind` is what the client hands to an executor the plugin's own
    JS registers (`PlexoraDropRouter.registerExecutor`) -- core never names a
    plugin route. None means "not mine", and must be cheap.
    """
    if fn is None:
        raise ValueError("register_sniffer needs a callable.")
    if fn not in _SNIFFERS:
        _SNIFFERS.append(fn)


class UnknownDatasource(KeyError):
    """The page named a project this server has not got."""


# -- the open project ----------------------------------------------------------

def _identity(src):
    try:
        return engine._identity(src) if src else None
    except (OSError, RuntimeError, ValueError):
        return None


def _signature(src):
    """`(name, size, mtime)` of a local file, or None. A flat table is COPIED
    into the project when attached (`import_routes._copy_into_project`, with
    its timestamps), so the copy is how the original is recognised later."""
    if not src or not _local(str(src)):
        return None
    try:
        stat = os.stat(src)
    except OSError:
        return None
    return Path(str(src)).name, stat.st_size, stat.st_mtime_ns


def _same_table(src, identity, signature):
    if not src:
        return False
    if identity and _identity(src) == identity:
        return True
    return signature is not None and _signature(src) == signature


def _snapshot(project):
    """What a drop is decided against, for one project. Internal fields carry
    a leading underscore and never reach the client."""
    names = list(project.image.channel_names)
    image_names = []
    try:
        image_names = engine._image_names(project.image.src, project.name)
    except Exception:  # noqa: BLE001 -- a name is evidence, not a requirement
        image_names = [project.name]
    return {
        "name": project.name,
        "title": project.display_name,
        "width": project.image.width,
        "height": project.image.height,
        "channelCount": len(names),
        "channelNames": names[:64],
        "genericNames": bool(names) and all(
            not str(n).strip() or GENERIC_CHANNEL.match(str(n).strip())
            for n in names),
        "hasMask": project.segmentation.requested,
        "hasTable": project.has_data_source,
        "_mask": _identity(project.segmentation.source),
        "_table": _identity(project.dataset.src if project.dataset else None),
        "_tableSignature": _signature(project.dataset.src if project.dataset else None),
        "_images": {i for i in (_identity(project.image.src),
                                _identity(project.image.source)) if i},
        "_layers": {i for i in (_identity(layer.src)
                                for layer in project.spatial_layers) if i},
        "_names": image_names,
    }


def _public(snapshot):
    if snapshot is None:
        return None
    return {key: value for key, value in snapshot.items()
            if not key.startswith("_")}


# -- names ---------------------------------------------------------------------

def _stem(path):
    """A file's sample stem with any segmentation suffix taken off."""
    stem = engine._group_stem(path)
    return _MASK_SUFFIX.sub("", stem) or stem


def _contains_word(haystack, needle):
    return bool(needle) and re.search(
        rf"(^|[^a-z0-9]){re.escape(needle)}([^a-z0-9]|$)", haystack) is not None


def name_match(path, names):
    """How a dropped file's name relates to an image's names.

    `exact` -- the same stem once the segmentation suffix is off
    (`LSP11385_cellmask.tif` over `LSP11385.ome.tif`); `prefix` -- one leads
    the other as a whole word (`LSP11385--unmicst_cell.tif`), or a folder the
    file sits in carries the image's name (`unmicst-LSP11385/cell.ome.tif`);
    `none` otherwise. Shared letters are not enough: `lsp11385` and
    `lsp11386` share seven and are two slides.
    """
    stem = _stem(path)
    wanted, folders_named = [], []
    for name in names or ():
        text = _stem(str(name)) if str(name) else ""
        if len(text) < 3:
            continue
        # A folder's name (`_ExactName`) names the image only exactly: two
        # slides in one `source/` folder are not related by sharing it.
        (folders_named if isinstance(name, engine._ExactName) else wanted).append(text)
    if not stem or not (wanted or folders_named):
        return "none"
    if stem in wanted or stem in folders_named:
        return "exact"
    for name in wanted:
        if engine._starts_with_word(stem, name) or engine._starts_with_word(name, stem):
            return "prefix"
    folders = [part.lower() for part in Path(str(path)).parent.parts[-3:]]
    for name in wanted:
        if any(_contains_word(folder, name) for folder in folders):
            return "prefix"
    return "none"


# -- what each kind of file is, relative to a project -------------------------

def _mask_size(row):
    geometry = row.geometry or {}
    width, height = geometry.get("width"), geometry.get("height")
    if width and height:
        return int(width), int(height)
    if not row.src or engine._is_node_address(row.src):
        return None
    from plexora.server.utils import segmentation_pyramid

    try:
        return segmentation_pyramid.plane_size(row.src)
    except Exception:  # noqa: BLE001
        return None


def _projects():
    from plexora.server.models.project import all_projects

    try:
        return all_projects()
    except Exception:  # noqa: BLE001
        return []


def _attached_to(src, field, exclude=None):
    """The project whose mask (`field="mask"`) or table is this file."""
    identity = _identity(src)
    if not identity:
        return None
    for project in _projects():
        if project.name == exclude:
            continue
        if field == "mask":
            if project.segmentation.source and \
                    _identity(project.segmentation.source) == identity:
                return project.name
        elif project.dataset is not None and _same_table(
                src, _identity(project.dataset.src),
                _signature(project.dataset.src)):
            return project.name
    return None


def _targets(path, size):
    """Projects a mask of this size could be the segmentation of: the image is
    the same size, best name match first, then the most recently opened."""
    if not size:
        return []
    found = []
    for project in _projects():
        if (project.image.width, project.image.height) != tuple(size):
            continue
        try:
            names = engine._image_names(project.image.src, project.name)
        except Exception:  # noqa: BLE001
            names = [project.name]
        match = name_match(path, names)
        rank = {"exact": 0, "prefix": 1}.get(match, 2)
        found.append((rank, -_opened(project), project.name, match,
                      project.segmentation.requested))
    found.sort()
    return [{"name": name, "match": match, "hasMask": has_mask}
            for _, _, name, match, has_mask in found[:TARGET_LIMIT]]


def _opened(project):
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(project.last_opened_at)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def mask_fit(row, snapshot):
    size = _mask_size(row)
    identity = _identity(row.src)
    fit = {"size": list(size) if size else None,
           "imageSize": None, "compatible": None, "nameMatch": "none",
           "alreadyAttached": False, "attachedTo": None, "targets": []}
    if snapshot is not None:
        image = (snapshot["width"], snapshot["height"])
        if all(image):
            fit["imageSize"] = list(image)
            if size:
                fit["compatible"] = tuple(size) == tuple(image)
        fit["nameMatch"] = name_match(_named(row), snapshot["_names"])
        fit["alreadyAttached"] = bool(identity) and identity == snapshot["_mask"]
    if not fit["alreadyAttached"]:
        fit["attachedTo"] = _attached_to(
            row.src, "mask", exclude=snapshot["name"] if snapshot else None)
    if snapshot is None or fit["compatible"] is False:
        fit["targets"] = _targets(_named(row), size)
    return fit


def _csv_columns(path):
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as handle:
            header = handle.readline().strip()
    except OSError:
        return []
    delimiter = next((d for d in ("\t", ",", ";") if d in header), ",")
    return [cell.strip().strip('"') for cell in header.split(delimiter)]


def table_fit(row, snapshot):
    from plexora.server.models.adapters import classify

    identity = _identity(row.src)
    fit = {"alreadyAttached": False, "attachedTo": None, "spatial": None,
           "multiImage": None, "replaces": bool(snapshot and snapshot["hasTable"])}
    if snapshot is not None:
        fit["alreadyAttached"] = _same_table(
            row.src, snapshot["_table"], snapshot["_tableSignature"])
    if not fit["alreadyAttached"]:
        fit["attachedTo"] = _attached_to(
            row.src, "table", exclude=snapshot["name"] if snapshot else None)
    src = str(row.src or "")
    local = src and not engine._is_node_address(src) and not is_remote_locator(src)
    suffix = Path(src).suffix.lower()
    if local and suffix in (".csv", ".tsv", ".txt"):
        roles = classify.guess_roles(_csv_columns(src))
        fit["spatial"] = bool(roles.get("x") and roles.get("y"))
    elif local and suffix == ".h5ad":
        try:
            from plexora.server.models.adapters import inspection

            spec = inspection.propose_read_spec(inspection.inspect_anndata(src))
            fit["spatial"] = bool(spec.get("coordinates"))
        except Exception:  # noqa: BLE001
            fit["spatial"] = None
    elif suffix == ".parquet":
        fit["spatial"] = True
    if row.subset and row.subset.get("values"):
        values = [str(v) for v in row.subset.get("values") or []]
        suggested = None
        if snapshot is not None:
            suggested = engine.subset_match(values, snapshot["_names"])
        fit["multiImage"] = {"column": row.subset.get("column"),
                             "count": len(values), "values": values[:200],
                             "suggested": suggested}
    return fit


def channels_fit(path, snapshot):
    from plexora.server.utils import channel_file

    sheet = engine._marker_sheet(path)
    fit = {"fits": False, "markerCount": None, "channelCount": None,
           "column": None, "hasHeader": None, "names": [], "current": []}
    if sheet is None:
        return fit
    grid, column, has_header = sheet
    count = snapshot["channelCount"] if snapshot else 0
    fit["channelCount"] = count or None
    if has_header is None and count:
        has_header = channel_file.autodetect(grid, count)
    if has_header is None:
        has_header = False
    try:
        names = channel_file.names(grid, column, has_header)
    except channel_file.ChannelFileError:
        names = []
    fit.update(column=column, hasHeader=bool(has_header),
               markerCount=len(names), names=names[:64])
    if snapshot is not None:
        fit["fits"] = bool(count) and len(names) == count
        fit["current"] = snapshot["channelNames"]
    return fit


# -- deciding ------------------------------------------------------------------

def _layers_route(sample, path, answers=None):
    return {"method": "POST", "path": "import/layers",
            "json": {"sample": sample, "paths": [path], "answers": answers or {}}}


def _open_route(path):
    """Opening a sample that already exists is navigation, not a request: the
    client goes to it by `sample`. (`/desktop/open` would IMPORT a mask or a
    table posted on its own as a new sample.)"""
    return None


def _channels_route(sample, path, fit, with_column=True):
    form = {"datasource": sample, "path": path}
    if with_column and fit.get("column") is not None:
        form["column"] = str(fit["column"])
        form["has_header"] = "true" if fit.get("hasHeader") else "false"
    return {"method": "POST", "path": "upload_channels", "form": form}


def _action(kind, item, status, message, *, reason=None, route=None,
            choices=None, default=None, sample=None, reload=False,
            navigate=False, fit=None):
    return {
        "id": "", "kind": kind, "status": status, "reason": reason,
        "message": message, "picks": [item["pick"]] if item else [],
        "paths": [item["path"]] if item else [], "sample": sample,
        "route": route, "choices": choices, "default": default,
        "one_press": False, "effects": {"reload": reload, "navigate": navigate},
        "then": [], "fit": fit if fit is not None else (item or {}).get("fit"),
        "label": (item or {}).get("label"),
    }


def _choice(value, label, kind, route=None, *, tone=None, reload=False,
            navigate=False, sample=None):
    return {"value": value, "label": label, "kind": kind, "route": route,
            "tone": tone, "sample": sample,
            "effects": {"reload": reload, "navigate": navigate}}


def _size(pair):
    return f"{pair[0]:,}×{pair[1]:,}" if pair else "unknown size"


def decide(item, open_):
    """The one action for one item, given the open project (a snapshot) or
    None. Pure: everything it reads is in `item["fit"]` and `open_`."""
    kind, fit, path = item["kind"], item.get("fit") or {}, item["path"]
    name = Path(str(path)).name
    sample = open_["name"] if open_ else None
    title = open_["title"] if open_ else None

    if kind == "project_dir":
        if open_ and fit.get("project") == sample:
            return _action("noop_already_open", item, "auto",
                           f"{title} is already open.")
        return _action("open_project", item, "auto",
                       f"Open {fit.get('project')}", route=_open_route(path),
                       sample=fit.get("project"), navigate=True)

    if kind == "channels":
        if open_ is None:
            return _action("apply_channel_names", item, "blocked",
                           f"{name} lists channel names. Open the sample they "
                           "name, then drop it on the image.",
                           reason="needs_open_sample")
        if not fit.get("fits"):
            return _action(
                "apply_channel_names", item, "needs_confirm",
                f"{name} lists {fit.get('markerCount') or 0} names; "
                f"{title} has {open_['channelCount']} channels.",
                reason="count_mismatch",
                route=_channels_route(sample, path, fit, with_column=False),
                sample=sample)
        if open_["genericNames"]:
            return _action("apply_channel_names", item, "auto",
                           f"Name {open_['channelCount']} channels from {name}",
                           route=_channels_route(sample, path, fit), sample=sample)
        return _action("apply_channel_names", item, "needs_confirm",
                       f"Replace the channel names of {title} with the "
                       f"{fit.get('markerCount')} in {name}?",
                       reason="names_present",
                       route=_channels_route(sample, path, fit), sample=sample)

    if kind == "mask":
        return _decide_mask(item, open_)

    if kind == "table":
        return _decide_table(item, open_)

    if kind == "image":
        return _decide_image(item, open_)

    if kind in ("bundle", "layer"):
        existing = fit.get("existing")
        if open_ and existing == sample:
            if kind == "layer" or (not fit.get("questions") and not fit.get("brings")):
                return _action("add_layer", item, "auto",
                               f"Add {name} to {title}",
                               route=_layers_route(sample, path), sample=sample,
                               reload=bool(fit.get("brings")))
            return _action("open_dialog", item, "needs_confirm",
                           f"Add {name} to {title}", reason="bundle_questions",
                           sample=sample)
        if existing:
            return _action("open_project", item, "auto",
                           f"Open {existing}", route=_open_route(path),
                           sample=existing, navigate=True)
        if open_ and kind == "layer":
            return _action("add_layer", item, "needs_confirm",
                           f"Add {name} to {title} as a layer?",
                           reason="adds_layer", route=_layers_route(sample, path),
                           sample=sample)
        if open_ and kind == "bundle" and not fit.get("withImage"):
            return _decide_imageless_bundle(item, open_)
        return _new_sample(item, open_)

    if kind == "folder":
        return _action("open_dialog", item, "auto", f"Import {name}",
                       reason="folder", sample=None)

    return _action("unsupported", item, "blocked",
                   f"Plexora does not know what to do with {name}.",
                   reason="unsupported")


def _new_sample(item, open_):
    fit = item.get("fit") or {}
    name = Path(str(item["path"])).name
    if fit.get("onePress"):
        action = _action(
            "import_new", item, "auto", f"Open {name} as a new sample",
            route={"method": "POST", "path": "import/sample",
                   "json": {"paths": [item["path"]], "key": fit.get("key") or ""}},
            navigate=True)
        action["one_press"] = True
        return action
    return _action("open_dialog", item, "auto", f"Import {name}",
                   reason="needs_dialog")


def _decide_imageless_bundle(item, open_):
    """A store with no image of its own -- shapes, cells, a mask -- dropped on
    an open sample. It cannot be a sample (`register_image_datasource` refuses
    a store with no image), so it is offered to the open one. Asked, never
    done on its own: nothing about a store says which slide it was made from,
    and what it brings may replace what the slide already has. A store with
    several tables is settled by the dialog `addToSample` opens."""
    fit, path = item.get("fit") or {}, item["path"]
    name = Path(str(path)).name
    sample, title = open_["name"], open_["title"]
    brings = fit.get("brings") or []
    replaces = [what for role, what, has in (
        ("mask", "a segmentation mask", open_["hasMask"]),
        ("table", "cell data", open_["hasTable"])) if role in brings and has]
    if replaces:
        message = (f"{title} already has {' and '.join(replaces)}. "
                   f"Replace with what {name} holds?")
        reason = "bundle_replaces"
    else:
        message = f"Add {name} to {title}?"
        reason = "bundle_to_open"
    return _action("add_layer", item, "needs_confirm", message, reason=reason,
                   route=_layers_route(sample, path), sample=sample,
                   reload="mask" in brings)


def _decide_image(item, open_):
    fit, path = item.get("fit") or {}, item["path"]
    name = Path(str(path)).name
    sample = open_["name"] if open_ else None
    title = open_["title"] if open_ else None
    if open_ and fit.get("sameAsOpen"):
        return _action("noop_already_open", item, "auto",
                       f"{name} is the image of {title}, already open.")
    if open_ and fit.get("inOpen"):
        return _action("noop_already_attached", item, "auto",
                       f"{name} is already a layer of {title}.")
    existing = fit.get("existing")
    if existing and existing != sample:
        return _action("open_project", item, "auto", f"Open {existing}",
                       route=_open_route(path), sample=existing, navigate=True)
    if open_ and fit.get("possiblyMask"):
        question = f"mask-or-image:{path}"
        choices = [
            _choice("mask", "Attach as segmentation mask", "attach_mask",
                    _layers_route(sample, path, {question: "mask"}), reload=True,
                    sample=sample),
            _choice("layer", "Add as an image layer", "add_layer",
                    _layers_route(sample, path, {question: "image"}),
                    sample=sample),
        ]
        if open_["hasMask"]:
            choices[0]["label"] = "Replace the segmentation mask"
            choices[0]["tone"] = "danger"
        return _action("attach_mask", item, "needs_confirm",
                       f"Is {name} a segmentation mask or an image?",
                       reason="ambiguous_pixels", choices=choices,
                       default="layer" if open_["hasMask"] else "mask",
                       sample=sample)
    if open_ and fit.get("nameMatch") in ("exact", "prefix"):
        choices = [
            _choice("layer", f"Add to {title} as a layer", "add_layer",
                    _layers_route(sample, path), sample=sample),
            _choice("new", "Open as a new sample", "import_new",
                    {"method": "POST", "path": "import/sample",
                     "json": {"paths": [path], "key": fit.get("key") or ""}}
                    if fit.get("onePress") else None, navigate=True),
        ]
        return _action("add_layer", item, "needs_confirm",
                       f"{name} looks related to {title}.",
                       reason="related_to_open", choices=choices,
                       default="layer", sample=sample)
    return _new_sample(item, open_)


def _decide_mask(item, open_):
    fit, path = item.get("fit") or {}, item["path"]
    name = Path(str(path)).name
    if open_ is None:
        if fit.get("withImage"):
            # Dropped with its image: one sample, imported (or reopened) together.
            return _new_sample(item, None)
        targets = fit.get("targets") or []
        if not targets:
            return _action("open_dialog", item, "auto",
                           f"{name} is a segmentation mask. Import it with "
                           "its image.", reason="mask_needs_image")
        first = targets[0]["name"]
        choices = [_choice(t["name"], t["name"], "attach_mask",
                           _layers_route(t["name"], path), reload=True,
                           sample=t["name"], tone="danger" if t["hasMask"] else None)
                   for t in targets]
        return _action("attach_mask", item, "needs_confirm",
                       f"Attach {name} to {first}?", reason="choose_target",
                       choices=choices, default=first, sample=first,
                       route=_layers_route(first, path), navigate=True)

    sample, title = open_["name"], open_["title"]
    attach = _layers_route(sample, path)
    as_layer = _layers_route(sample, path, {f"mask-or-image:{path}": "image"})
    if fit.get("alreadyAttached"):
        return _action("noop_already_attached", item, "auto",
                       f"{name} is already the segmentation mask of {title}.")
    if fit.get("attachedTo"):
        other = fit["attachedTo"]
        return _action(
            "attach_mask", item, "needs_confirm",
            f"{name} is the segmentation mask of {other}.",
            reason="attached_elsewhere", sample=sample, default="open",
            choices=[
                _choice("open", f"Open {other}", "open_project",
                        _open_route(path), navigate=True, sample=other),
                _choice("attach", f"Attach to {title} anyway", "attach_mask",
                        attach, reload=True, sample=sample),
            ])
    compatible, match = fit.get("compatible"), fit.get("nameMatch")
    if compatible is False:
        choices = [_choice("attach", "Attach anyway", "attach_mask", attach,
                           reload=True, sample=sample),
                   _choice("layer", "Add as an image layer", "add_layer",
                           as_layer, sample=sample)]
        best = next((t for t in fit.get("targets") or [] if t["name"] != sample), None)
        if best is not None:
            choices.insert(0, _choice(
                "target", f"Attach to {best['name']} (same size)", "attach_mask",
                _layers_route(best["name"], path), reload=True, navigate=True,
                sample=best["name"]))
        return _action(
            "attach_mask", item, "needs_confirm",
            f"{name} is {_size(fit.get('size'))}; {title} is "
            f"{_size(fit.get('imageSize'))}.", reason="size_mismatch",
            choices=choices, default=choices[0]["value"] if best else None,
            sample=sample)
    if open_["hasMask"]:
        return _action(
            "attach_mask", item, "needs_confirm",
            f"{title} already has a segmentation mask.", reason="mask_exists",
            sample=sample, default=None,
            choices=[
                _choice("replace", "Replace mask", "attach_mask", attach,
                        reload=True, tone="danger", sample=sample),
                _choice("layer", "Add as an image layer", "add_layer", as_layer,
                        sample=sample),
            ])
    auto = (compatible is True and match in ("exact", "prefix")) or (
        compatible is None and match == "exact")
    if auto:
        return _action("attach_mask", item, "auto",
                       f"Attach segmentation mask to {title}", route=attach,
                       sample=sample, reload=True)
    reason = "name_mismatch" if compatible is True else "size_unknown"
    return _action("attach_mask", item, "needs_confirm",
                   f"Attach {name} to {title}?", reason=reason, route=attach,
                   sample=sample, reload=True)


def _decide_table(item, open_):
    fit, path = item.get("fit") or {}, item["path"]
    name = Path(str(path)).name
    if open_ is None:
        if fit.get("withImage"):
            return _new_sample(item, None)
        if fit.get("existing"):
            return _action("open_project", item, "auto",
                           f"Open {fit['existing']}", route=_open_route(path),
                           sample=fit["existing"], navigate=True)
        if fit.get("attachedTo"):
            return _action("open_project", item, "auto",
                           f"Open {fit['attachedTo']}", route=_open_route(path),
                           sample=fit["attachedTo"], navigate=True)
        return _new_sample(item, None)

    sample, title = open_["name"], open_["title"]
    if fit.get("alreadyAttached"):
        return _action("noop_already_attached", item, "auto",
                       f"{name} is already the cell data of {title}.")
    multi = fit.get("multiImage") or None
    answers = {}
    if multi and multi.get("suggested"):
        answers[f"subset:{sample}"] = multi["suggested"]
    attach = _layers_route(sample, path, answers)
    if fit.get("attachedTo"):
        other = fit["attachedTo"]
        return _action(
            "attach_table", item, "needs_confirm",
            f"{name} is the cell data of {other}.", reason="attached_elsewhere",
            sample=sample, default="open",
            choices=[
                _choice("open", f"Open {other}", "open_project",
                        None, navigate=True, sample=other),
                _choice("attach", f"Attach to {title} anyway", "attach_table",
                        attach, sample=sample),
            ])
    if multi and not multi.get("suggested"):
        return _action("open_dialog", item, "needs_confirm",
                       f"{name} holds cells of {multi['count']} images. Which "
                       f"one is {title}?", reason="subset_unknown",
                       sample=sample)
    if open_["hasTable"]:
        return _action("attach_table", item, "needs_confirm",
                       f"{title} already has cell data.", reason="table_exists",
                       route=attach, sample=sample)
    if fit.get("spatial") is False:
        return _action("attach_table", item, "needs_confirm",
                       f"{name} has no cell coordinates Plexora recognises.",
                       reason="no_coordinates", route=attach, sample=sample)
    message = f"Attach cell data to {title}"
    if multi:
        message = f"Attach the cells of {multi['suggested']} to {title}"
    return _action("attach_table", item, "auto", message, route=attach,
                   sample=sample)


#: The order actions run in. Nothing that reloads the page or leaves it may run
#: before something that only changes the record: names, then cells, then
#: layers, then the mask (which reloads), then plugin kinds, then navigation.
_ORDER = {
    "noop_already_open": 0, "noop_already_attached": 0, "unsupported": 0,
    "apply_channel_names": 1, "attach_table": 2, "add_layer": 3,
    "attach_mask": 4, "open_dialog": 7, "import_new": 8, "open_project": 9,
}


def _order(actions):
    return sorted(actions, key=lambda a: (_ORDER.get(a["kind"], 5),
                                          min(a["picks"] or [0])))


# -- inspecting ----------------------------------------------------------------

def _local(path):
    text = str(path)
    return bool(text) and not engine._is_node_address(text) \
        and not is_remote_locator(text)


def _precheck(picks, node):
    """`(picks with failures blanked, unrecognised)`. Blanks keep positions:
    every row's `pick` is an index into the list the client sent."""
    kept, refused = [], []
    for index, raw in enumerate(picks):
        text = str(raw or "").strip()
        if not text or node or not _local(text):
            kept.append(text)
            continue
        path = Path(text).expanduser()
        code = None
        if not path.exists():
            code, reason = "missing", f"{path.name} is not there any more."
        elif not os.access(path, os.R_OK):
            code, reason = "unreadable", f"{path.name} cannot be read (permissions)."
        if code:
            refused.append({"pick": index, "path": text, "reason": reason,
                            "code": code})
            kept.append("")
        else:
            kept.append(text)
    return kept, refused


def _named(row):
    return engine._named_by(row)


def _mtimes(picks):
    stamps = []
    for raw in picks:
        try:
            stamps.append(os.stat(raw).st_mtime_ns if raw and _local(raw) else 0)
        except OSError:
            stamps.append(-1)
    return tuple(stamps)


def inspect_drop(paths, *, datasource=None, target=None, node=None):
    """The plan for dropping `paths` on `target` with `datasource` open.

    @raises UnknownDatasource when `datasource` is named and not registered.
    """
    from plexora.server.models.project import Project

    picks = [str(p or "").strip() for p in (paths or [])]
    open_project = None
    if datasource:
        open_project = Project.find(datasource)
        if open_project is None:
            raise UnknownDatasource(datasource)

    key = (tuple(picks), datasource or "", target or "", node or "",
           _mtimes(picks), _record_stamp())
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_SECONDS:
            return hit[1]

    plan = _inspect(picks, open_project, target, node)
    with _CACHE_LOCK:
        _CACHE[key] = (now, plan)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.pop(next(iter(_CACHE)))
    return plan


def forget():
    """Drop every cached plan (after a drop has changed what is registered)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _record_stamp():
    """Changes whenever any config.json does, so a cached plan never outlives
    the registration it was decided against."""
    from plexora import paths as plexora_paths

    stamps = []
    for root in plexora_paths.roots():
        try:
            stamps.append(os.stat(plexora_paths.config_path(root)).st_mtime_ns)
        except OSError:
            stamps.append(0)
    return tuple(stamps)


def _inspect(picks, open_project, target, node):
    from plexora.server.models.project import Project

    snapshot = _snapshot(open_project) if open_project is not None else None
    plan = {"target": target, "datasource": snapshot["name"] if snapshot else None,
            "open": _public(snapshot), "items": [], "actions": [],
            "unrecognised": [], "warnings": [], "proposal": None,
            "summary": {}}
    kept, refused = _precheck(picks, node)
    plan["unrecognised"].extend(refused)
    items, actions = [], []

    # Project folders, channel lists and plugin kinds: decided before the
    # engine, and kept from it -- a project folder would be read as its own
    # derived pyramids, a gates sheet as a table of cells.
    engine_picks = list(kept)
    context = {"open": _public(snapshot), "target": target}
    channel_picks = []
    for index, raw in enumerate(kept):
        if not raw or node or not _local(raw):
            continue
        path = Path(raw).expanduser()
        project = Project.at_directory(path) if path.is_dir() else None
        if project:
            items.append({"pick": index, "path": raw, "kind": "project_dir",
                          "label": project, "detail": "Plexora project",
                          "fit": {"project": project}})
            engine_picks[index] = ""
            continue
        if not path.is_file():
            continue
        claimed = _sniffed(raw, context)
        if claimed is not None:
            item = {"pick": index, "path": raw, "kind": claimed["kind"],
                    "label": claimed.get("label") or path.name,
                    "detail": claimed.get("detail") or "",
                    "fit": claimed.get("fit") or {}}
            items.append(item)
            spec = claimed.get("action") or {}
            action = _action(spec.get("kind") or claimed["kind"], item,
                             spec.get("status") or "needs_confirm",
                             spec.get("message") or path.name,
                             reason=spec.get("reason"), route=spec.get("route"),
                             sample=spec.get("sample"))
            action["effects"].update(spec.get("effects") or {})
            actions.append(action)
            engine_picks[index] = ""
            continue
        if engine._marker_sheet(path) is not None:
            channel_picks.append(index)

    proposal = None
    if any(engine_picks):
        proposal = engine.inspect_paths(engine_picks, node=node)
        plan["proposal"] = proposal.to_dict()
        plan["warnings"].extend(proposal.warnings)

    engine_items = _engine_items(proposal, engine_picks, snapshot) if proposal else []
    items.extend(engine_items)
    for index in channel_picks:
        items.append({"pick": index, "path": kept[index], "kind": "channels",
                      "label": Path(kept[index]).name, "detail": "Channel names",
                      "fit": channels_fit(kept[index], snapshot)})

    # Unrecognised: the engine's, less the channel lists it refused only
    # because it was not asked to name anything with them.
    if proposal is not None:
        for entry in proposal.unrecognised:
            if entry.get("pick") in channel_picks:
                continue
            plan["unrecognised"].append({**entry, "code": "unsupported"})

    images_go_elsewhere = False
    claimed = {a["picks"][0] for a in actions}
    for item in items:
        if item["pick"] in claimed:
            continue
        decided = decide(item, snapshot)
        if decided["kind"] in ("import_new", "open_dialog") and item["kind"] != "channels":
            images_go_elsewhere = True
        actions.append(decided)

    # A channel list dropped with images that become a NEW sample names that
    # sample's channels (the engine already applied it); it is not also a
    # rename of whatever is open.
    if images_go_elsewhere:
        actions = [a for a in actions if a["kind"] != "apply_channel_names"]

    actions = _merge_new_samples(actions, proposal, engine_picks)
    if snapshot is None:
        actions = _with_followers(actions, items)
    actions = _order(actions)
    for number, action in enumerate(actions):
        action["id"] = f"a{number}"
    plan["items"] = items
    plan["actions"] = actions
    plan["summary"] = _summary(plan)
    return plan


def _sniffed(path, context):
    for sniffer in _SNIFFERS:
        try:
            found = sniffer(path, context)
        except Exception:  # noqa: BLE001 -- a plugin's bug is not the drop's
            found = None
        if found:
            return found
    return None


def _engine_items(proposal, picks, snapshot):
    """One item per pick the engine recognised: a run folder is one `bundle`,
    a folder of loose files one `folder`, a file one item of its own kind."""
    by_pick = {}
    for number, sample in enumerate(proposal.samples):
        for row in sample.layers:
            if row.pick is None:
                continue
            by_pick.setdefault(row.pick, []).append((number, sample, row))
    one_press = (len(proposal.samples) == 1
                 and not proposal.warnings and not proposal.unrecognised
                 and not any(s.questions for s in proposal.samples))
    items = []
    for pick in sorted(by_pick):
        rows = by_pick[pick]
        number, sample, row = rows[0]
        path = picks[pick]
        common = {"existing": sample.existing, "key": sample.key,
                  "withImage": any(r.reference for r in sample.layers),
                  "sampleIndex": number,
                  "onePress": one_press,
                  "questions": len(sample.questions)}
        if any(r.bundle for _, _, r in rows):
            brings = sorted({r.role for _, _, r in rows if r.role in ("mask", "table")})
            items.append({"pick": pick, "path": path, "kind": "bundle",
                          "label": Path(path).name,
                          "detail": (row.bundle or {}).get("format") or "",
                          "fit": {**common, "brings": brings,
                                  "layers": len(rows)}})
            continue
        if len(rows) > 1:
            items.append({"pick": pick, "path": path, "kind": "folder",
                          "label": Path(path).name,
                          "detail": f"{len(rows)} files", "fit": common})
            continue
        items.append(_row_item(pick, path, row, common, snapshot))
    return items


def _row_item(pick, path, row, common, snapshot):
    label = Path(str(path)).name
    if row.role == "mask":
        return {"pick": pick, "path": path, "kind": "mask", "label": label,
                "detail": row.detail, "fit": {**common, **mask_fit(row, snapshot)}}
    if row.role == "table":
        return {"pick": pick, "path": path, "kind": "table", "label": label,
                "detail": row.detail,
                "fit": {**common, **table_fit(row, snapshot)}}
    if row.kind == "image":
        identity = _identity(row.src)
        fit = {**common, "possiblyMask": any(
            str(need).startswith("mask-or-image:") for need in row.needs),
            "sameAsOpen": False, "inOpen": False, "nameMatch": "none"}
        if snapshot is not None:
            fit["sameAsOpen"] = bool(identity) and identity in snapshot["_images"]
            fit["inOpen"] = bool(identity) and identity in snapshot["_layers"]
            fit["nameMatch"] = name_match(_named(row), snapshot["_names"])
            if fit["existing"] == snapshot["name"]:
                fit["sameAsOpen"] = True
        if fit["possiblyMask"] and row.geometry and snapshot is not None:
            size = (row.geometry.get("width"), row.geometry.get("height"))
            fit["size"] = list(size)
            fit["imageSize"] = [snapshot["width"], snapshot["height"]]
        return {"pick": pick, "path": path, "kind": "image", "label": label,
                "detail": row.detail, "fit": fit}
    return {"pick": pick, "path": path, "kind": "layer", "label": label,
            "detail": row.detail, "fit": common}


def _merge_new_samples(actions, proposal, picks):
    """Several picks that all go to the importer are ONE import: the files of
    one sample are registered together, and several samples are the dialog's
    to lay out."""
    going = [a for a in actions if a["kind"] in ("import_new", "open_dialog")
             and a.get("sample") is None and a["status"] != "needs_confirm"]
    if len(going) <= 1 or proposal is None:
        return actions
    rest = [a for a in actions if a not in going]
    paths = [p for p in picks if p]
    merged = dict(going[0])
    merged["picks"] = sorted({p for a in going for p in a["picks"]})
    merged["paths"] = [picks[p] for p in merged["picks"]]
    if len(proposal.samples) == 1 and all(a["one_press"] for a in going):
        merged.update(kind="import_new", message=f"Open {proposal.samples[0].name}",
                      route={"method": "POST", "path": "import/sample",
                             "json": {"paths": paths,
                                      "key": proposal.samples[0].key}})
    else:
        merged.update(kind="open_dialog", route=None, one_press=False,
                      message=(f"Import {len(proposal.samples)} samples"
                               if len(proposal.samples) > 1 else
                               f"Import {proposal.samples[0].name}"),
                      reason="needs_dialog")
    return rest + [merged]


def _with_followers(actions, items):
    """With nothing open: when one file of a drop names a project that already
    has it (its image, say), the rest of that drop is decided against that
    project and carried as `then`, so the page that opens attaches them."""
    from plexora.server.models.project import Project

    opens = [a for a in actions if a["kind"] == "open_project" and a["sample"]]
    if len(opens) != 1:
        return actions
    opener = opens[0]
    project = Project.find(opener["sample"])
    if project is None:
        return actions
    snapshot = _snapshot(project)
    leftovers = [i for i in items if i["pick"] not in opener["picks"]
                 and i["kind"] in ("mask", "table", "channels")]
    if not leftovers:
        return actions
    follow = []
    for item in leftovers:
        fit = dict(item["fit"])
        if item["kind"] == "mask":
            refit = _refit_mask(item, snapshot)
            fit.update(refit)
        elif item["kind"] == "channels":
            fit = channels_fit(item["path"], snapshot)
        elif item["kind"] == "table":
            fit["alreadyAttached"] = _same_table(
                item["path"], snapshot["_table"], snapshot["_tableSignature"])
            fit["replaces"] = snapshot["hasTable"]
            fit["attachedTo"] = None
            if fit.get("multiImage"):
                fit["multiImage"] = {**fit["multiImage"], "suggested":
                                     engine.subset_match(fit["multiImage"]["values"],
                                                         snapshot["_names"])}
        decided = decide({**item, "fit": fit}, snapshot)
        if decided["kind"] in ("open_project", "import_new", "open_dialog"):
            continue
        follow.append(decided)
    picks = {p for a in follow for p in a["picks"]}
    rest = [a for a in actions if a is opener or not (set(a["picks"]) & picks)]
    opener = dict(opener)
    opener["then"] = _order(follow)
    for number, action in enumerate(opener["then"]):
        action["id"] = f"t{number}"
    if follow:
        opener["status"] = "needs_confirm"
        opener["reason"] = "open_and_attach"
        opener["message"] = (f"Open {opener['sample']} and add "
                             f"{len(follow)} file{'s' if len(follow) != 1 else ''}?")
    return [opener if a["kind"] == "open_project" and a["sample"] == opener["sample"]
            else a for a in rest]


def _refit_mask(item, snapshot):
    path = item["path"]
    size = (item["fit"] or {}).get("size")
    image = (snapshot["width"], snapshot["height"])
    return {
        "imageSize": list(image) if all(image) else None,
        "compatible": (tuple(size) == tuple(image)) if size and all(image) else None,
        "nameMatch": name_match(path, snapshot["_names"]),
        "alreadyAttached": _identity(path) == snapshot["_mask"],
        "attachedTo": None,
    }


def _summary(plan):
    """One line for the hover label, and whether one press does it all."""
    actions = [a for a in plan["actions"] if a["status"] != "blocked"]
    blocked = [a for a in plan["actions"] if a["status"] == "blocked"]
    tone = "ok"
    if not actions:
        hint = (blocked[0]["message"] if blocked else
                (plan["unrecognised"][0]["reason"] if plan["unrecognised"]
                 else "Nothing Plexora can read here."))
        tone = "refused"
    elif len(actions) == 1:
        hint = _hint(actions[0])
        if actions[0]["status"] == "needs_confirm":
            tone = "ask"
    else:
        hint = f"Add {len(actions)} files" if plan["datasource"] else \
            f"Open {len(actions)} files"
        if any(a["kind"] == "open_dialog" for a in actions):
            hint = "Import files"
        if any(a["status"] == "needs_confirm" for a in actions):
            tone = "ask"
    return {
        "one_press": bool(actions) and all(a["status"] == "auto" for a in actions),
        "needs_dialog": any(a["kind"] == "open_dialog" for a in actions),
        "hint": hint, "tone": tone,
    }


_HINTS = {
    "attach_mask": "Attach segmentation mask",
    "attach_table": "Attach single-cell data",
    "apply_channel_names": "Import channel names",
    "add_layer": "Add as a layer",
    "open_project": "Open project",
    "import_new": "Open image",
    "open_dialog": "Import",
    "noop_already_open": "Already open",
    "noop_already_attached": "Already attached",
}


def _hint(action):
    base = _HINTS.get(action["kind"])
    if base is None:
        return action["message"]
    sample = action.get("sample")
    if action["kind"] in ("attach_mask", "attach_table", "add_layer") and sample:
        return f"{base} to {sample}"
    if action["kind"] == "open_project" and sample:
        return f"Open {sample}"
    return base
