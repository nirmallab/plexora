"""Registering data nodes, and pointing a project's resources at them.

The public counterpart of `plexora.datasource`: that module registers a project
whose files are on this machine, and this one says that one of those files is
somewhere else instead.

    import plexora

    plexora.register_node("hpc", "http://compute-3:8642", token="...")
    plexora.attach_table("tonsil", node="hpc", resource_id="cells")

Nothing here moves data or copies it. `attach_table` sends the project's read
spec to the node, has it read the file once, and records what came back: a
generation, a fingerprint, and the shape of the table. The image, the mask and
the table are attached independently, so "image on the cluster, table on the
laptop" is two calls rather than a mode.

**A project's meaning stays here.** The node is told how to read a file and is
never told what the project is called, what its roles are for, or that a
project exists at all. That is what keeps one authoritative Plexora database:
lose a node and you lose access to bytes, never to work.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

from plexora.server.models import nodes as node_registry
from plexora.server.models.adapters import inspection as data_inspection
from plexora.server.models.nodes import Node
from plexora.server.models.project import (
    RESOURCE_KINDS,
    ColumnGroups,
    DataSpec,
    Project,
    ResourceBinding,
)
from plexora.server.providers import http


def _now():
    return datetime.datetime.now().isoformat()


def register_node(name, endpoint, token=None, browser_endpoint=None, verify=True,
                  managed_by=None, role=None, expires_at=None):
    """Record how to reach a data node, and check that it answers.

    `endpoint` is how THIS machine reaches it. `browser_endpoint` is how the
    user's browser does, when that is a different address -- an Open OnDemand
    portal path (`/rnode/compute-3/8642/`), or a tunnelled loopback port. Leave
    it unset whenever the two are the same, which is the desktop and Docker
    case and most tunnels.

    `verify=False` records the node without contacting it. For the case the
    check cannot cover: registering a node that is not up yet, from a script
    that starts it afterwards.

    `managed_by` marks an entry that a saved connection set up and will set up
    again -- e.g. "connect:hpc". It changes nothing here; it is what lets the
    settings page say "this one comes back by itself" instead of inviting
    somebody to repair an address that is rewritten every session anyway.

    `role="client"` says this node runs on the machine the BROWSER is on. Sent
    only by `plexora connect`, which is the only thing that can know it -- see
    `Node.role`. It is what lets a data form offer "Local" and mean the user's
    own computer.

    `expires_at` is when the job serving this node runs out, as a Unix time.
    Recorded HERE rather than left on the session because the two things have
    different lifetimes: a node outlives the process that started it, so after
    a restart the tunnel is up, the session is gone, and this entry is the only
    thing left that knows there is a clock at all.
    """
    if not name or not str(name).strip():
        raise ValueError("a node needs a name -- it is what a project points at")
    extra = {}
    if managed_by:
        extra["managed_by"] = str(managed_by)
    if role:
        extra["role"] = str(role)
    if expires_at:
        extra["expires_at"] = float(expires_at)
    node = Node(
        name=str(name).strip(),
        endpoint=str(endpoint).rstrip("/"),
        token=str(token or ""),
        browser_endpoint=(str(browser_endpoint).rstrip("/") if browser_endpoint else None),
        extra=extra,
    )
    if verify:
        hello = http.hello(node, timeout=10.0)
        offered = hello.get("api_version")
        if offered != node_registry.API_VERSION:
            raise ValueError(
                f"node {name!r} speaks node API {offered}; this Plexora needs "
                f"{node_registry.API_VERSION}. Upgrade whichever end is older.")
        node = replace(node, api_version=offered, node_id=hello.get("node_id"),
                       plexora_version=hello.get("plexora_version"),
                       last_seen=_now())
    return node_registry.save(node)


def forget_node(name):
    """Remove a node. Projects pointing at it will report it unreachable."""
    node_registry.remove(str(name))


def list_nodes():
    """Every registered node, with what it last said about itself."""
    return list(node_registry.load_all().values())


def node_resources(name):
    """What a node is serving right now, as it describes itself."""
    return http.hello(node_registry.get(str(name)), timeout=10.0).get("resources") or []


def client_node():
    """The registered node running on the machine the BROWSER is on, or None.

    Record-only: no node is contacted. Whether it is answering right now is a
    different question with a different lifetime, and asking it here would make
    every page load wait on a laptop that may have gone to sleep.

    There is at most one, because there is at most one browser. `plexora
    connect` re-registers it under the same name every session, so a second
    entry cannot accumulate -- and if somehow two exist, the first by name is
    as good an answer as any and better than refusing to offer the option.
    """
    for entry in sorted(node_registry.load_all().values(), key=lambda n: n.name):
        if entry.role == "client":
            return entry
    return None


def resource_id_for(path) -> str:
    """The id a node will serve `path` under.

    Derived from the path rather than generated, and that is the whole point:
    it has to come out the same next week. A project records the id in its
    binding, the node records it in its manifest, and the two only meet again
    because both were computed from the same filename -- nothing is exchanged
    between sessions to reconcile them.

    Readable half plus a hash of the whole path, because neither alone works: a
    bare filename collides the moment somebody shares `cells.csv` from two
    directories, and a bare hash makes every message about a resource
    unreadable ("node://laptop/7f3a91c2" tells a user nothing).
    """
    import hashlib
    import re

    text = str(path).strip()
    stem = Path(text).name or "file"
    # `.ome.tif`, `.h5ad`, `.zarr` -- one suffix strip, so `cells.h5ad` and
    # `cells.csv` stay distinguishable by the hash rather than colliding here.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", Path(stem).stem).strip("-").lower()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{slug or 'file'}-{digest}"


def share_path(node, kind, path):
    """Have a node start serving a file on ITS machine, and say what it is.

    The counterpart of `--serve` for a node that is already running: the user
    picks a file on their own computer from a form in a browser, long after the
    node started, and this is how the viewer tells the node about it.

    Returns the node's description of the resource, which carries the `state`
    -- a segmentation mask may need converting before it can serve a tile, and
    the caller polls `resource_status` until it says otherwise.

    `path` is a path on the NODE's filesystem and is never stored here: a
    binding that carried another machine's mount points would be wrong the
    moment it was read anywhere else (see providers/base.py). The id that comes
    back is what gets recorded.
    """
    entry = node_registry.get(str(node))
    resource_id = resource_id_for(path)
    answer = http.json_request(
        entry, "POST", "/node/v1/resources",
        body={"kind": str(kind), "id": resource_id, "path": str(path)},
        timeout=120.0, expected_api=node_registry.API_VERSION)
    described = dict(answer.get("resource") or {})
    described.setdefault("id", resource_id)
    described["locator"] = f"node://{entry.name}/{described['id']}"
    return described


def resource_status(node, resource_id):
    """Whether a node can read one of its resources yet, and why not if not."""
    entry = node_registry.get(str(node))
    answer = http.json_request(
        entry, "GET", f"/node/v1/resources/{resource_id}/status",
        timeout=30.0, expected_api=node_registry.API_VERSION)
    described = dict(answer.get("resource") or {})
    described["locator"] = f"node://{entry.name}/{resource_id}"
    return described


def unshare_path(node, resource_id):
    """Stop a node serving one resource. Nothing on its disk is touched."""
    entry = node_registry.get(str(node))
    return http.json_request(
        entry, "DELETE", f"/node/v1/resources/{resource_id}",
        timeout=30.0, expected_api=node_registry.API_VERSION)


def browse_on_node(node, mode="file", file_filter="any"):
    """Open a file dialog on the NODE's machine and return the chosen path.

    None when the user cancelled, which is a real answer and not a failure.

    The dialog opens where the desktop is, which on the layout this exists for
    is the user's own laptop -- while this process is on a compute node with no
    display. Nothing is read: what comes back is a path, and it means nothing
    here until somebody asks that same node to serve it (`share_path`).
    """
    entry = node_registry.get(str(node))
    answer = http.json_request(
        entry, "POST", "/node/v1/browse",
        body={"mode": str(mode), "filter": str(file_filter)},
        # Generous, because on the other end of this is a person looking at a
        # dialog. The node's own picker timeout is what really bounds it.
        timeout=360.0, expected_api=node_registry.API_VERSION)
    return answer.get("path")


def list_dir_on_node(node, path="", show_hidden=False):
    """One directory on the NODE's machine, as its picker draws it.

    What "Browse" means on a host with no desktop -- which is every cluster.
    `browse_on_node` opens a dialog where somebody is sitting; this walks a
    filesystem where nobody is. Same promise either way: a path comes back,
    never bytes, and it means nothing here until that node is asked to serve
    it (`share_path`).

    The keys are copied out by name rather than passed through whole, so a node
    running a newer build cannot inject fields the picker never asked for --
    which does mean anything the picker learns to draw has to be added here.
    """
    entry = node_registry.get(str(node))
    answer = http.json_request(
        entry, "POST", "/node/v1/list_dir",
        body={"path": str(path or ""), "show_hidden": bool(show_hidden)},
        timeout=30.0, expected_api=node_registry.API_VERSION)
    return {key: answer.get(key) for key in
            ("path", "parent", "crumbs", "entries", "truncated")}


def open_file_on_node(node, path, timeout=600.0):
    """One file on the NODE's machine, as an unread stream.

    The exception to the promise the two functions above make. `browse_on_node`
    and `list_dir_on_node` return a path and never bytes, because until now
    naming a file was all a remote machine had to do -- something over here
    then opened it. This is for the case where nothing over here can: a
    plugin's Upload button wants the FILE, and the browser asking for it is on
    a third machine with no route to the node at all.

    The response is handed back unread and the caller must consume and release
    it. Streamed rather than buffered because these are the files people keep
    on a cluster because they are large.
    """
    entry = node_registry.get(str(node))
    return http.request(
        entry, "POST", "/node/v1/read_file", body={"path": str(path or "")},
        stream=True, timeout=timeout,
        expected_api=node_registry.API_VERSION)


def write_file_on_node(node, directory, name, stream, size=None,
                       overwrite=False, timeout=600.0):
    """Put one file onto the NODE's machine. Returns what it wrote.

    `{"path", "bytes"}` on success, and `{"exists": True, "error": ...}` when
    there is already a file of that name -- which is a question to put to the
    user rather than a failure, so it comes back as an answer instead of an
    exception (see `http.request`'s `allow_status`).

    `stream` is read while the socket is written, so an export of a whole cell
    table never sits in this process. The name is sent as a query parameter and
    checked on the far side, where the filesystem that has to accept it is.
    """
    entry = node_registry.get(str(node))
    query = urlencode({
        "dir": str(directory or ""), "name": str(name or ""),
        "overwrite": "1" if overwrite else "0",
    })
    headers = {"Content-Length": str(int(size))} if size is not None else None
    response = http.request(
        entry, "POST", f"/node/v1/write_file?{query}",
        raw_body=stream, headers=headers, timeout=timeout,
        expected_api=node_registry.API_VERSION, allow_status=(409,))

    try:
        answer = json.loads(response.data or b"{}")
    except ValueError:
        answer = {}
    if response.status == 409:
        return {"success": False, "exists": bool(answer.get("exists")),
                "error": answer.get("error") or "the node refused the write"}
    return answer


def inspect_table(name, resource_id, table=None):
    """What a node's table file offers, before deciding how to read it.

    The same document the local import screen works from -- obs columns, obsm
    arrays, layers, the proposed read spec -- so the questions about a remote
    file are asked in exactly the words they are asked about a local one.
    """
    node = node_registry.get(str(name))
    query = f"?table={table}" if table else ""
    return http.json_request(
        node, "GET", f"/node/v1/table/{resource_id}/inspect{query}",
        timeout=120.0, expected_api=node_registry.API_VERSION)


# -- pointing a project at a node ----------------------------------------


def attach_table(project, node, resource_id, spec=None, table=None,
                 subset_column=None, subset_value=None, reinspect=False,
                 **spec_fields):
    """Point a project's cell table at a node, and load it once.

    The project keeps every answer about what the table MEANS -- which column
    is the cell id, which matrix holds the intensities, whether the values are
    log-transformed. Those travel to the node with each load and are never
    stored there.

    `spec` (or the `spec_fields` keywords) describes how to read the file. If
    neither is given and the project already has a table spec, that one is
    reused with its `src` pointed at the node -- which is the ordinary way to
    move an existing project's table onto a node without re-answering anything.

    `subset_column`/`subset_value` name the one image's worth of rows to read
    out of a table that spans several. They are asked on the import form and on
    the edit page, and used to be dropped on the floor for a node table -- so a
    file covering twelve slides loaded all twelve, and every coordinate landed
    somewhere plausible and wrong.

    `reinspect=True` asks the node to look at the file afresh rather than
    reusing the project's existing spec. That is the difference between the two
    surfaces that get here: the Edit page's "where the data lives" picker says
    *this same table now lives there*, while its Data field says *read this
    other file instead*, and reusing a CSV's spec to read an .h5ad is how the
    second one silently corrupts a project.

    Returns the updated Project.
    """
    project = _project(project)
    entry = node_registry.get(str(node))

    read_spec, derived = _read_spec_for(project, spec, table, spec_fields,
                                        entry, resource_id, reinspect=reinspect)
    read_spec = _with_subset(read_spec, subset_column, subset_value)
    described = http.json_request(
        entry, "POST", f"/node/v1/table/{resource_id}/load",
        body={"spec": read_spec.to_dict(), "reload": True},
        timeout=600.0, expected_api=node_registry.API_VERSION)

    if derived:
        # A spec worked out from the node's inspection has never been through
        # an adapter, so the marker/metadata split and the file's own column,
        # layer and obsm lists are still blank. The load that just ran on the
        # node is the same adapter pass the local import records them from --
        # so record its answers, and the project comes out indistinguishable
        # from one whose table was imported here.
        markers = tuple(described.get("feature_columns") or ())
        read_spec = replace(
            read_spec,
            columns=ColumnGroups(
                markers=markers,
                metadata=tuple(c for c in described.get("columns") or ()
                               if c not in set(markers)),
            ),
            obs_columns=tuple(described.get("obs_columns") or ()),
            layers=tuple(described.get("layers") or ()),
            obsm=tuple(dict(entry_) for entry_ in described.get("obsm") or ()),
        )

    binding = ResourceBinding(
        kind="table",
        provider="node",
        node=entry.name,
        resource_id=str(resource_id),
        fingerprint=described.get("fingerprint"),
        capabilities=tuple(_capabilities(entry)),
    )
    # The spec is stored with the node's own path stripped out: `src` names a
    # file on the NODE's filesystem, and a project record that carried one
    # machine's mount points would be wrong the moment it was read anywhere
    # else. The binding says where the file is; the spec says how to read it.
    read_spec = replace(read_spec, src=f"node://{entry.name}/{resource_id}")
    updated = project.patch(dataset=read_spec).with_resource("table", binding)
    updated.save()
    return _reload(updated.name)


def attach_image(project, node, resource_id, channel_names=None):
    """Point a project's image at a node.

    The geometry -- dimensions, pyramid depth, tile size, channel count --
    comes back from the node and is recorded centrally, because every one of
    those is something the viewer needs before it can ask for a single tile.
    The channel NAMES stay the project's: renaming a panel is a thing users do
    on the primary, and the node never needs to hear about it.
    """
    project = _project(project)
    entry = node_registry.get(str(node))
    geometry = http.json_request(
        entry, "GET", f"/node/v1/image/{resource_id}/geometry",
        timeout=120.0, expected_api=node_registry.API_VERSION)
    _same_image(project, geometry)

    from plexora.datasource import _image_channel_entries

    count = geometry["num_channels"]
    names = list(channel_names or [])
    if not names:
        names = [f"{resource_id}_{index}" for index in range(count)]
    if len(names) != count:
        raise ValueError(
            f"the image on node {entry.name!r} has {count} channels but "
            f"{len(names)} names were given")

    # The tile-URL key for each plane. `<resource>_<N>` on purpose: the tile
    # route parses the trailing number to get the pyramid index, and the node
    # parses the identical string -- so the index travels in the URL the client
    # already builds and nothing has to be looked up at either end.
    channel_info = {
        "channel_names": [f"{resource_id}_{index}" for index in range(count)],
        "num_channels": count,
    }

    image = replace(
        project.image,
        src="",
        channels=tuple(_image_channel_entries(
            project.name, channel_info, names, project.segmentation.derived)),
        width=geometry["width"],
        height=geometry["height"],
        max_level=geometry["levels"],
        tile_width=geometry["tile_width"],
        tile_height=geometry["tile_height"],
        num_channels=geometry["num_channels"],
    )
    binding = ResourceBinding(
        kind="image", provider="node", node=entry.name,
        resource_id=str(resource_id),
        capabilities=tuple(_capabilities(entry)),
    )
    updated = project.patch(image=image).with_resource("image", binding)
    updated.save()
    return _reload(updated.name)


def attach_segmentation(project, node, resource_id):
    """Point a project's mask at a node.

    The node makes its own mask servable at startup -- converting it where it
    lies if it has to -- so by the time it is offering the resource there is a
    label pyramid behind it. What that conversion cannot decide from over here
    is which KIND of pyramid it is, so the mode is read off the node's own
    description rather than assumed: a filled pyramid and an outline pyramid
    both serve tiles happily and draw different, wrong pictures if the viewer
    is told the wrong one.
    """
    from plexora.datasource import _with_area_channel
    from plexora.server.utils import segmentation_pyramid

    project = _project(project)
    entry = node_registry.get(str(node))
    hello = _handshake(entry)
    binding = ResourceBinding(
        kind="segmentation", provider="node", node=entry.name,
        resource_id=str(resource_id),
        capabilities=tuple(hello.get("capabilities") or []),
    )
    derived = f"node://{entry.name}/{resource_id}"
    segmentation = replace(
        project.segmentation, derived=derived,
        status="ready",
        mode=(_mask_mode(hello, resource_id) or project.segmentation.mode
              or segmentation_pyramid.DEFAULT_MODE),
    )
    # The viewer's label layer is `imageData[0]`, so the placeholder goes in
    # here too. Without it a project could attach a mask on a node, record it,
    # serve its tiles correctly -- and draw the first real image channel as the
    # label layer, because nothing downstream asks where the mask came from.
    image = replace(project.image, channels=tuple(
        _with_area_channel(project.name, project.image.channels, derived)))
    updated = project.patch(image=image, segmentation=segmentation).with_resource(
        "segmentation", binding)
    updated.save()
    return _reload(updated.name)


def _same_image(project, geometry):
    """Refuse to point an existing project at a DIFFERENT image.

    Where the primary image lives can change -- reaching the same file through
    a node instead of from disk is the ordinary reason a project is repointed,
    and is exactly what a laptop coming and going needs. What that image IS
    cannot change: every ROI outline, every figure panel and every cell
    coordinate this project holds is expressed in that image's pixel space, and
    an image of another size would leave all of it rendering perfectly and
    meaning something else. Nothing downstream would report an error, because
    nothing downstream is in a position to notice.

    Dimensions and channel count rather than a fingerprint, deliberately: the
    same slide converted, re-tiled or copied between filesystems is a different
    file and the same image, and refusing that would forbid the move this whole
    feature exists to allow.

    A project with no image yet -- one being created -- has nothing to disagree
    with, and is let through.
    """
    current = project.image
    if not (current.width and current.height):
        return
    if (current.width == geometry["width"]
            and current.height == geometry["height"]
            and current.num_channels == geometry["num_channels"]):
        return
    raise ValueError(
        f"{project.name!r} was built on an image of "
        f"{current.width}x{current.height} with {current.num_channels} "
        f"channels, and that one is {geometry['width']}x{geometry['height']} "
        f"with {geometry['num_channels']}. Where the image lives can change; "
        f"which image it is cannot -- every ROI, figure and cell coordinate in "
        f"this project is in its pixel space. Import a new project instead.")


def _with_subset(read_spec, column, value):
    """`read_spec` restricted to one image's rows, when a column was named.

    Applied after the spec is chosen rather than inside each branch of
    `_read_spec_for`, because it is the same answer whether the spec came from
    the project, from the caller or from the node's own inspection -- and a
    subset that only survived one of those three routes is the kind of gap that
    reads as "it works" right up until the file spans several slides.

    A blank column is not an instruction to clear an existing subset: the
    import form and the edit page both omit the field when the file does not
    span several images, and treating that as "load everything" would drop the
    answer every time an unrelated setting was saved.
    """
    if not column:
        return read_spec
    return replace(read_spec, subset={"column": str(column),
                                      "value": str(value or "")})


def _mask_mode(hello, resource_id):
    """"filled"/"outlines" for a node's mask, or None if it did not say.

    None falls back to whatever the project already had and then to the
    default, and the second half of that matters: `SegmentationSpec.mode`
    starts as None, and a None mode is not written to config.json at all.
    Missing is not "unknown" downstream -- `canDrawFilled` and
    `renderLabelTile` both test `segmentationMode === "filled"`, so absent
    reads as outlines, which greys Filled out and paints a filled pyramid as
    solid blobs. An older node that reports nothing is far likelier to be
    serving filled labels than outlines, so that is the guess to make.
    """
    for described in hello.get("resources") or []:
        if described.get("id") == str(resource_id):
            return described.get("mask_mode")
    return None


#: Where a user sets a local path for each resource, named in the refusal
#: below. A message that says "provide a path" and does not say where is a
#: message that sends somebody hunting through a form.
_LOCAL_PATH_FIELD = {
    "image": "the image cannot be repointed -- import a new project instead",
    "segmentation": "the Segmentation Mask field on this project's Edit page",
    "table": "the Data field on this project's Edit page",
}


def detach(project, kind, path=None):
    """Bring a resource back to this machine.

    `path` is where the file is HERE, and it is required: a project whose table
    is on a node has no local copy by construction, so removing the binding
    without saying what replaces it would leave the project pointing at
    `node://…` with nothing to read it. Refusing names the field to use
    instead, which is the only actionable thing to say.

    Removing a binding never touches the project's own answers -- roles, the
    marker split, the coordinate source, the log switch. That is the whole
    reason the binding is a separate record from the path: a table that comes
    home is not a table that has to be re-imported.
    """
    if kind not in RESOURCE_KINDS:
        raise KeyError(f"Unknown resource kind: {kind!r}")
    project = _project(project)
    binding = project.resource(kind)
    if binding is None:
        return project

    path = str(path).strip() if path else ""
    if not path and kind != "segmentation":
        raise ValueError(
            f"{project.name}'s {kind} is on node {binding.node!r}, so there is "
            f"no copy of it on this machine. Say where it is using "
            f"{_LOCAL_PATH_FIELD[kind]}, or copy the file here first.")

    updated = project.with_resource(kind, None)
    if kind == "table" and updated.dataset is not None:
        updated = updated.patch(dataset=replace(updated.dataset, src=path))
    elif kind == "segmentation":
        # An empty path is a real answer here and the only one of the three
        # where it is: "this project no longer has a mask" is something a user
        # legitimately means, and the edit page already expresses it by
        # clearing the field.
        from plexora.datasource import _with_area_channel

        updated = updated.patch(
            image=replace(updated.image, channels=tuple(_with_area_channel(
                updated.name, updated.image.channels, path))),
            segmentation=replace(
                updated.segmentation, derived=path or None, source=path or None,
                source_key=None, status="ready"))
    elif kind == "image":
        # The same guard the node path applies, for an image coming home. The
        # read is a real one and is worth it: this happens once, by hand, and
        # the failure it prevents is silent.
        from plexora.server.providers import local as local_providers

        try:
            _same_image(project, local_providers.image_geometry(path))
        except (OSError, KeyError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError(f"{path} could not be read as an image: {exc}")
        updated = updated.patch(image=replace(updated.image, src=path))
    updated.save()
    return _reload(updated.name)


# -- internals ------------------------------------------------------------


def _project(project) -> Project:
    return project if isinstance(project, Project) else Project.load(str(project))


def _handshake(entry):
    """The node's `/hello`, or an empty dict if it will not answer.

    Swallowing the failure is deliberate: everything read out of here is
    describing the node, and a node that cannot be reached at this moment is a
    reason to record less about it, not a reason to refuse to attach.
    """
    try:
        return http.hello(entry, timeout=10.0) or {}
    except Exception:
        return {}


def _capabilities(entry):
    """What the node says it can run, recorded so a control can be offered or
    not rather than offered and then failing."""
    return _handshake(entry).get("capabilities") or []


def dialogs_on_node(node):
    """What kind of file dialog the NODE can put on a screen, or None.

    One of native_dialog's HYBRID/KINDS/NONE, straight from `/hello`. Asked
    only after that node has refused to open a dialog, to tell "there is no
    desktop over there" from "there is a desktop, and two dialogs on it, just
    not one that takes a file AND a folder" -- which decides whether Browse
    offers the in-app listing or asks which kind and opens the real thing.

    None whenever the node will not say: too old to carry the field, or not
    reachable this second. Both mean the caller should do what it did before
    the field existed, which is why this never raises -- it is asked while
    already handling a failure, and a probe that threw would replace a refusal
    somebody can act on with one nobody can.
    """
    try:
        return _handshake(node_registry.get(str(node))).get("dialogs")
    except Exception:
        return None


def _read_spec_for(project, spec, table, spec_fields, entry,
                   resource_id, reinspect=False) -> tuple[DataSpec, bool]:
    """The spec to load under, and whether it was derived from scratch.

    The flag matters to the caller: a spec that came off the node's inspection
    has never been through an adapter, so the caller records the column split
    the load reports -- while a spec the project (or the caller) already owned
    keeps its own recorded answers untouched.

    `reinspect` skips the project's own spec entirely -- see `attach_table`.
    """
    if spec is not None:
        built = spec if isinstance(spec, DataSpec) else DataSpec.from_dict(dict(spec))
        return built, False
    if spec_fields:
        fields = dict(spec_fields)
        if table:
            fields["table"] = table
        fields.setdefault("src", f"node://{entry.name}/{resource_id}")
        fields.setdefault("type", "anndata")
        built = DataSpec.from_dict(fields)
        if built is None:
            raise ValueError("the read spec needs at least a `type`")
        return built, False
    if project.dataset is not None and not reinspect and not table:
        return project.dataset, False
    if project.dataset is not None and not reinspect:
        return replace(project.dataset, table=table), False
    # No spec anywhere: ask the node to look at the file and propose one --
    # the same inspection and role-guessing the local import screen runs, so
    # a table that has never been imported can still be attached. This is the
    # ordinary situation for the laptop-share layout: the viewer runs beside
    # the images and the .h5ad has never been anywhere near it.
    document = inspect_table(entry.name, resource_id, table=table)
    fields = data_inspection.spec_from_inspection(document)
    fields.setdefault("src", f"node://{entry.name}/{resource_id}")
    built = DataSpec.from_dict(fields)
    if built is not None:
        return built, True
    raise ValueError(
        f"{project.name!r} has no table spec yet, and the node's inspection "
        f"of {resource_id!r} did not yield one"
        + (" (a SpatialData file needs table=... to say which of its tables "
           "to read)" if document.get("tables") else "")
        + ". Pass spec=... (see plexora.nodes.inspect_table).")


def _reload(name):
    """Re-read the project through the provider layer, so the running server
    picks the change up without a restart."""
    from plexora.server.models import data_model

    data_model.load_datasource(name, reload=True)
    return Project.load(name)
