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

    A data node is a `plexora node serve` process running on another machine
    -- a cluster, a lab workstation -- that can serve image, mask and table
    files to a viewer running elsewhere. Registering one reads and copies no
    data; it only records how to reach it, so a project can later be pointed
    at one of its files with `plexora.attach_table`, `plexora.attach_image`
    or `plexora.attach_segmentation`.

    When to use:
        Call this once per node, typically before attaching anything to it.
        Registering the same name again replaces the existing entry.

    Args:
        name (str): A short name for the node. Every other function in this
            module refers to the node by this name.
        endpoint (str): The URL this machine uses to reach the node, e.g.
            `"http://compute-3:8642"`.
        token (str, optional): The node's bearer token, if it requires one.
        browser_endpoint (str, optional): The URL the user's BROWSER uses to
            reach the node, when that is a different address -- an Open
            OnDemand portal path (`/rnode/compute-3/8642/`), or a tunnelled
            loopback port. Leave it unset whenever the two are the same,
            which is the desktop and Docker case and most tunnels.
        verify (bool, optional): Contact the node before recording it, and
            check that it speaks a node API this installation understands.
            Defaults to `True`. Pass `False` to record a node that is not up
            yet, from a script that starts it afterwards.
        managed_by (str, optional): A label recording who keeps this entry
            current, e.g. `"connect:hpc"` for one a saved connection will set
            up again. Advisory only; nothing here reads it back.
        role (str, optional): `"client"` to mark the node as the one running
            on the machine the browser is on. Leave unset for an ordinary
            node.
        expires_at (float, optional): When the process serving this node is
            expected to stop, as a Unix timestamp.

    Returns:
        Node: The saved node record. When `verify=True`, it carries the
        `api_version`, `node_id`, `plexora_version` and `last_seen` the
        handshake reported.

    Raises:
        ValueError: If `name` is blank, or (with `verify=True`) the node
            answers with a node-API version this installation does not
            speak.

    Example:
        ```python
        import plexora

        plexora.register_node("hpc", "http://compute-3:8642", token="...")
        ```
    """
    if not name or not str(name).strip():
        raise ValueError("a node needs a name -- it is what a project points at")
    extra = {}
    if managed_by:
        # Changes nothing here; it is what lets the settings page say "this
        # one comes back by itself" instead of inviting somebody to repair an
        # address that is rewritten every session anyway.
        extra["managed_by"] = str(managed_by)
    if role:
        # Sent only by `plexora connect`, which is the only thing that can
        # know it -- see `Node.role`. It is what lets a data form offer
        # "Local" and mean the user's own computer.
        extra["role"] = str(role)
    if expires_at:
        # Recorded HERE rather than left on the session because the two
        # things have different lifetimes: a node outlives the process that
        # started it, so after a restart the tunnel is up, the session is
        # gone, and this entry is the only thing left that knows there is a
        # clock at all.
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
    """Remove a registered node.

    Forgetting a node does not touch any project's resource bindings: a
    project that had a table, an image or a mask attached to this node keeps
    pointing at it, and reports that resource unreachable until either the
    node is registered again under the same name (`plexora.register_node`) or
    the resource is brought back with `plexora.detach`.

    Args:
        name (str): The registered node's name. Forgetting a name that is not
            registered does nothing.

    Example:
        ```python
        import plexora

        plexora.forget_node("hpc")
        ```
    """
    node_registry.remove(str(name))


def list_nodes():
    """List every registered node, with what it last said about itself.

    Reads the local registry only -- nothing here contacts a node, so an
    entry for a node that is asleep or disconnected comes back the same as
    one that answered a moment ago.

    Returns:
        list[Node]: One record per registered node, each with `name`,
        `endpoint`, `token` and `browser_endpoint`, plus -- once a handshake
        has succeeded at least once -- `api_version`, `node_id`,
        `plexora_version` and `last_seen`.

    Example:
        ```python
        import plexora

        for node in plexora.list_nodes():
            print(node.name, node.endpoint, node.last_seen)
        ```
    """
    return list(node_registry.load_all().values())


def node_resources(name):
    """List the files a node is serving right now, as it describes itself.

    Contacts the node's handshake endpoint, so this is a live answer -- what
    the node is serving at this moment -- rather than anything cached here.

    Args:
        name (str): The registered node's name, as given to
            `plexora.register_node`.

    Returns:
        list[dict]: One entry per resource the node is serving, each with at
        least `id`, `kind` (`"image"`, `"segmentation"` or `"table"`),
        `generation`, `loaded`, `state`, `error` and `fingerprint`. A
        `segmentation` entry also carries `mask_mode`, `warning` and
        `progress`; an `image` entry also carries `image_type` and
        `image_type_reason`.

    Raises:
        KeyError: If no node named `name` is registered.

    Example:
        ```python
        import plexora

        for resource in plexora.node_resources("hpc"):
            print(resource["kind"], resource["id"], resource["state"])
        ```
    """
    return http.hello(node_registry.get(str(name)), timeout=10.0).get("resources") or []


def image_type_on_node(name, resource_id):
    """`(verdict, reason)` for an image a node is serving, or `(None, None)`.

    Read out of `/hello` rather than the geometry endpoint on purpose: this
    answers a question the upload form asks while somebody is still typing, and
    `describe()` reports what the node worked out when the resource was added,
    so nothing here opens a pyramid.
    """
    for described in node_resources(name):
        if str(described.get("id")) == str(resource_id):
            return (described.get("image_type") or None,
                    described.get("image_type_reason") or None)
    return None, None


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


def detect_on_node(node, path):
    """What a node makes of one path on its own disk, before serving it.

    `{"kind": ..., "mask": ..., "reason": ...}`, where `kind` is None for
    something no node can serve -- a folder, a file Plexora does not read --
    and `reason` is the sentence to show for it.

    Asked instead of guessing from the name, because the name is not enough:
    `cell.ome.tif` out of an mcmicro run is a segmentation mask and says so
    nowhere, and the dtype and plane count that do say so are readable only
    over there.
    """
    entry = node_registry.get(str(node))
    answer = http.json_request(
        entry, "POST", "/node/v1/detect", body={"path": str(path)},
        timeout=120.0, expected_api=node_registry.API_VERSION)
    return dict(answer.get("detected") or {})


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


def share_path_replacing(node, kind, path, owner_of=None):
    """`share_path`, first taking back a stale registration of the same file.

    A node registers a file once, under one kind, for as long as it runs --
    and its manifest carries that across restarts. So a mask browsed to in
    the import dialog, re-read there as an image and then removed, stayed
    registered as an IMAGE; asking for the same path as a segmentation mask
    from the viewer later was refused with "this node already serves a
    different resource called 'cellring-ome-…'", which names a thing the user
    cannot see and gives them nothing to do.

    Replaced when nothing reads it: the registration is this server's own
    leftover, and serving the file as what it is now being asked for is the
    only sensible outcome. Refused, saying which project, when one does --
    pulling a project's image out from under it is a much larger act than
    attaching a mask.

    @param owner_of - `(node, resource_id) -> project name or None`. Passed
        in, because what reads a resource is the primary's config and this
        module is below it.
    """
    from plexora.server.providers.base import ResourceError

    resource_id = resource_id_for(path)
    served = {str(entry.get("id")): entry for entry in node_resources(node)}
    described = served.get(resource_id)
    if described is not None and str(described.get("kind")) != str(kind):
        owner = owner_of(node, resource_id) if owner_of else None
        if owner:
            raise ResourceError(
                f"{node} already serves {Path(str(path)).name} as the "
                f"{described.get('kind')} of the project {owner!r}. Use it "
                "there, or copy the file.")
        unshare_path(node, resource_id)
    return share_path(node, kind, path)


def resource_status(node, resource_id, timeout=30.0):
    """Whether a node can read one of its resources yet, and why not if not."""
    entry = node_registry.get(str(node))
    answer = http.json_request(
        entry, "GET", f"/node/v1/resources/{resource_id}/status",
        timeout=timeout, expected_api=node_registry.API_VERSION)
    described = dict(answer.get("resource") or {})
    described["locator"] = f"node://{entry.name}/{resource_id}"
    return described


def prepare_again(node, resource_id, timeout=30.0):
    """Ask a node to retry converting a mask whose last conversion failed.

    Returns the node's description, normally `preparing` by now; poll
    `resource_status` from there. A node too old to have the route answers
    with the "upgrade it there" sentence `http._check` writes for any 404.
    """
    entry = node_registry.get(str(node))
    answer = http.json_request(
        entry, "POST", f"/node/v1/resources/{resource_id}/prepare",
        timeout=timeout, expected_api=node_registry.API_VERSION)
    described = dict(answer.get("resource") or {})
    described["locator"] = f"node://{entry.name}/{resource_id}"
    return described


def pyramidize_on_node(node, resource_id, timeout=30.0):
    """Ask a node to build the pyramidized copy of a flat image it serves.

    The copy is written on the node, beside the original or under the node's
    own data root. Returns the node's description, normally `preparing`; poll
    `resource_status` until it is `ready` (served from the copy) or `error`.
    A node too old to have the route answers with the "upgrade it there"
    sentence `http._check` writes for any 404.
    """
    entry = node_registry.get(str(node))
    answer = http.json_request(
        entry, "POST", f"/node/v1/resources/{resource_id}/pyramidize",
        timeout=timeout, expected_api=node_registry.API_VERSION)
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
    """Inspect a table file a node is serving, before deciding how to read it.

    Has the node look at the file's structure and returns the same kind of
    document a local import screen works from, so the questions about a
    remote file ("which column is the cell id?") are asked in exactly the
    words they are asked about a local one. `plexora.attach_table` calls this
    itself when it has nothing else to build a read spec from.

    Args:
        name (str): The registered node's name.
        resource_id (str): The table resource's id on that node (see
            `plexora.node_resources`).
        table (str, optional): Which table to inspect, for a file that holds
            more than one (a SpatialData `.zarr` store).

    Returns:
        dict: The node's structural inspection of the file -- `data_type`,
        `obs_columns`, `obsm_keys`, `layers` and a proposed read spec under
        `proposed`, plus `tables` when the file holds more than one table and
        none was named in `table`.

    Raises:
        KeyError: If no node named `name` is registered.

    Example:
        ```python
        import plexora

        document = plexora.inspect_table("hpc", "cells")
        ```
    """
    node = node_registry.get(str(name))
    query = f"?table={table}" if table else ""
    return http.json_request(
        node, "GET", f"/node/v1/table/{resource_id}/inspect{query}",
        timeout=120.0, expected_api=node_registry.API_VERSION)


# -- pointing a project at a node ----------------------------------------


#: Why an attach might not want to load the project afterwards.
#:
def attach_table(project, node, resource_id, spec=None, table=None,
                 subset_column=None, subset_value=None, reinspect=False,
                 reload=True, **spec_fields):
    """Point a project's cell table at a file a node is serving, and load it once.

    The project keeps every answer about what the table MEANS -- which column
    is the cell id, which matrix holds the intensities, whether the values are
    already log-transformed. Those travel to the node with each load and are
    never stored there; only the read spec and the loaded shape travel back.

    When to use:
        Call this to move an existing project's table onto a node: pass just
        `project`, `node` and `resource_id`, and the project's existing table
        spec is reused. Pass `spec` (or the spec fields as keywords) to
        attach a table the project has never had before. The image, the mask
        and the table are attached independently -- see
        `plexora.attach_image` and `plexora.attach_segmentation` -- so
        "table on the cluster, image on the laptop" is two calls, not a mode.

    Args:
        project (str | Project): The project to update, by name or object.
        node (str): The registered node serving the file, as given to
            `plexora.register_node`.
        resource_id (str): The table resource's id on that node (see
            `plexora.node_resources`).
        spec (dict | DataSpec, optional): How to read the file. When neither
            `spec` nor any `spec_fields` are given and the project already
            has a table spec, that spec is reused with its source pointed at
            the node. When the project has no spec yet either, the node is
            asked to inspect the file and propose one (see
            `plexora.inspect_table`).
        table (str, optional): Which table to read, for a file that holds
            more than one (a SpatialData `.zarr` store).
        subset_column (str, optional): The obs column that names which
            image's rows belong to this project, for a file that spans
            several images.
        subset_value (str, optional): The value of `subset_column` that
            selects this project's rows.
        reinspect (bool, optional): Ask the node to inspect the file afresh
            instead of reusing the project's existing spec. Use this when the
            file at `resource_id` is not the file the project's spec was
            built for -- reusing a CSV's spec to read an `.h5ad` reads the
            wrong columns silently.
        reload (bool, optional): Reload the project in this process after
            saving. Defaults to `True`; pass `False` when a different process
            is serving the project and will be told to reload separately.
        **spec_fields: `DataSpec` fields (`type`, `src`, `coordinates`,
            `features`, `obs_id_field`, `normalization`, ...) to build a read
            spec from scratch, as an alternative to `spec`.

    Returns:
        Project: The project, saved with the table pointed at the node and
        (unless `reload=False`) reloaded so this process serves it.

    Raises:
        KeyError: If `project` names an unknown project, or `node` names an
            unregistered node.
        ValueError: If no read spec can be worked out for the table -- pass
            `spec` or the spec fields.

    Example:
        ```python
        import plexora

        plexora.attach_table("tonsil", node="hpc", resource_id="cells")
        ```
    """
    project = _project(project)
    entry = node_registry.get(str(node))

    read_spec, derived = _read_spec_for(project, spec, table, spec_fields,
                                        entry, resource_id, reinspect=reinspect)
    # `subset_column`/`subset_value` are asked on the import form and on the
    # edit page, and used to be dropped on the floor for a node table -- so a
    # file covering twelve slides loaded all twelve, and every coordinate
    # landed somewhere plausible and wrong.
    #
    # `reinspect` is the difference between the two surfaces that get here:
    # the Edit page's "where the data lives" picker says *this same table now
    # lives there*, while its Data field says *read this other file instead*
    # -- and reusing a CSV's spec to read an .h5ad is how the second one
    # would otherwise silently corrupt a project.
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
    return _reload(updated.name) if reload else updated


def _node_image_kind(current, effective):
    """The `image_kind` to record for an image served from a node.

    `effective` is the reading actually being recorded -- the user's override
    where there is one, the node's verdict otherwise.

    Three answers rather than two, because "nobody said" is a real case: a node
    older than this detection omits the key and no override supplies one, and
    reading that as fluorescence would silently flip a project that had been
    repointed from a local H&E file. Absence leaves the project's own kind
    alone; a definite answer replaces it.

    'brightfield' and 'ome_tiff' are the only two values written here. A
    fluorescence image is named for the container it came out of, and a node
    image's container is not something the primary opened -- 'ome_tiff' is what
    every node-backed project has recorded since before this existed.
    """
    from plexora.server.models.project import (IMAGE_TYPE_BRIGHTFIELD,
                                               IMAGE_TYPE_FLUORESCENCE)

    if effective == IMAGE_TYPE_BRIGHTFIELD:
        return IMAGE_TYPE_BRIGHTFIELD
    if effective == IMAGE_TYPE_FLUORESCENCE and current == IMAGE_TYPE_BRIGHTFIELD:
        # The one case worth rewriting: this project was being served as colour
        # and the file it now reads through says it is a channel stack. Leaving
        # `brightfield` would keep one `rgb` layer in front of an image the node
        # will only serve a plane at a time.
        return "ome_tiff"
    return current or "ome_tiff"


def attach_image(project, node, resource_id, channel_names=None,
                 image_type=None, reload=True):
    """Point a project's image at a file a node is serving.

    The geometry -- dimensions, pyramid depth, tile size, channel count --
    comes back from the node and is recorded here, because the viewer needs
    all of it before it can ask for a single tile. The channel NAMES stay the
    project's own: renaming a panel is something a user does on the primary,
    and the node never needs to hear about it.

    Whether the image is brightfield or a fluorescence channel stack is also
    decided here, from the node's own detection -- a `node://` address is not
    something this process can open to run the usual detector -- unless
    `image_type` overrides it.

    Args:
        project (str | Project): The project to update, by name or object.
        node (str): The registered node serving the file.
        resource_id (str): The image resource's id on that node.
        channel_names (list[str], optional): A name for each channel, in
            order. Ignored for a brightfield image, which has one layer.
            Defaults to `"<resource_id>_<index>"` for each channel.
        image_type (str, optional): `"brightfield"` to read the file as an
            RGB image regardless of what the node detected. Any other value
            is read as a fluorescence channel stack. Leave unset to use the
            node's own detection. An override given here is remembered, so
            re-attaching later -- a laptop that came back, a project
            repointed at another node -- reads the image the same way
            without being told again.
        reload (bool, optional): Reload the project in this process after
            saving. Defaults to `True`.

    Returns:
        Project: The project, saved with the image pointed at the node and
        (unless `reload=False`) reloaded.

    Raises:
        KeyError: If `project` names an unknown project, or `node` names an
            unregistered node.
        ValueError: If `channel_names` is given with a different length than
            the node reports channels, or if the image the node serves does
            not match the project's existing one in size and channel count --
            attaching a DIFFERENT image is refused, because every ROI, figure
            and cell coordinate in the project is expressed in that image's
            pixel space.

    Example:
        ```python
        import plexora

        plexora.attach_image("tonsil", node="hpc", resource_id="slide")
        ```
    """
    from plexora.server.models.project import IMAGE_TYPE_BRIGHTFIELD
    from plexora.server.utils import brightfield

    project = _project(project)
    entry = node_registry.get(str(node))
    geometry = http.json_request(
        entry, "GET", f"/node/v1/image/{resource_id}/geometry",
        timeout=120.0, expected_api=node_registry.API_VERSION)
    # Without a node-side detector, an H&E slide reached through a node used
    # to come out as a three-channel fluorescence project: R, G and B offered
    # as markers and composited additively on black. The node runs the same
    # detector a local import would and reports its answer in this same
    # geometry response (see `node/resources.py`).
    #
    # `image_type`, when given, needs nothing of the node: a node's pyramid
    # presents (channel, y, x) whichever way it opened the file,
    # `brightfield.rgb_region` stacks three of those planes for a brightfield
    # tile when the level has no `.rgb` of its own, and the geometry recorded
    # here is whatever the node will actually serve -- so the two readings
    # are both self-consistent rather than one being a special case.
    _same_image(project, geometry)

    from plexora.datasource import _image_channel_entries

    count = geometry["num_channels"]
    detected = geometry.get("image_type")
    choice = image_type or project.image.image_type_choice
    # The same precedence `convertOmeTiff` applies: a choice outranks the
    # detector, and "no choice" is what makes the detector's answer the answer.
    # Guarded on the plane count for the reason `_with_enough_planes` states --
    # a brightfield reading of a one- or two-plane image has nothing to put in
    # the third sample, and a monochrome slide read as colour would be three
    # borrowed markers.
    effective = choice or detected
    if effective == IMAGE_TYPE_BRIGHTFIELD and count < 3:
        effective = None

    if effective == IMAGE_TYPE_BRIGHTFIELD:
        # One servable layer keyed `rgb`, exactly what `_convert_brightfield_image`
        # records for the same slide on this machine -- the sentinel is what
        # makes the tile route hand back the file's own colour samples instead
        # of quantizing a plane, and the node parses the identical string.
        #
        # `num_channels` stays 3 below: it counts the planes the pyramid has,
        # which is what `_same_image` compares against. How many layers the
        # viewer draws is this list, and for brightfield that is one.
        #
        # Any `channel_names` passed in are dropped rather than applied. There
        # is nothing to name -- three samples are one picture -- and the local
        # path says the same (see `datasource.reregister_image`).
        channel_info = {"channel_names": [brightfield.RGB_CHANNEL_KEY],
                        "num_channels": count}
        names = ["Image"]
    else:
        names = list(channel_names or [])
        if not names:
            # The names the node read out of the file itself, when it is new
            # enough to send them; the tile keys otherwise, as before.
            own = geometry.get("channel_names")
            if isinstance(own, list) and len(own) == count:
                names = [str(name) for name in own]
        if not names:
            names = [f"{resource_id}_{index}" for index in range(count)]
        if len(names) != count:
            raise ValueError(
                f"the image on node {entry.name!r} has {count} channels but "
                f"{len(names)} names were given")

        # The tile-URL key for each plane. `<resource>_<N>` on purpose: the tile
        # route parses the trailing number to get the pyramid index, and the
        # node parses the identical string -- so the index travels in the URL
        # the client already builds and nothing has to be looked up at either
        # end.
        channel_info = {
            "channel_names": [f"{resource_id}_{index}" for index in range(count)],
            "num_channels": count,
        }

    image = replace(
        project.image,
        src="",
        kind=_node_image_kind(project.image.kind, effective),
        image_type_choice=choice or None,
        channels=tuple(_image_channel_entries(
            project.name, channel_info, names, project.segmentation.derived)),
        width=geometry["width"],
        height=geometry["height"],
        max_level=geometry["levels"],
        tile_width=geometry["tile_width"],
        tile_height=geometry["tile_height"],
        num_channels=geometry["num_channels"],
        # What the node concluded and why, on the same terms a local import
        # records them: the edit page shows the reason, so somebody can judge
        # whether the answer is worth disagreeing with.
        image_type_detected=detected or None,
        image_type_reason=geometry.get("image_type_reason") or None,
    )
    binding = ResourceBinding(
        kind="image", provider="node", node=entry.name,
        resource_id=str(resource_id),
        capabilities=tuple(_capabilities(entry)),
    )
    updated = project.patch(image=image).with_resource("image", binding)
    updated.save()
    return _reload(updated.name) if reload else updated


def attach_segmentation(project, node, resource_id, reload=True):
    """Point a project's segmentation mask at a file a node is serving.

    Whether the node's mask is a filled pyramid or an outline pyramid is read
    off the node's own description rather than assumed: both serve tiles
    happily, and drawing one as the other paints a wrong picture without
    raising any error.

    Args:
        project (str | Project): The project to update, by name or object.
        node (str): The registered node serving the file.
        resource_id (str): The segmentation resource's id on that node (see
            `plexora.node_resources`).
        reload (bool, optional): Reload the project in this process after
            saving. Defaults to `True`.

    Returns:
        Project: The project, saved with the mask pointed at the node and
        (unless `reload=False`) reloaded.

    Raises:
        KeyError: If `project` names an unknown project, or `node` names an
            unregistered node.

    Example:
        ```python
        import plexora

        plexora.attach_segmentation("tonsil", node="hpc", resource_id="mask")
        ```
    """
    from plexora.datasource import _with_area_channel
    from plexora.server.utils import segmentation_pyramid

    project = _project(project)
    entry = node_registry.get(str(node))
    # The node makes its own mask servable at startup -- converting it where
    # it lies if it has to -- so by the time it is offering the resource
    # there is a label pyramid behind it. What that conversion cannot decide
    # from over here is which KIND of pyramid it is, which is why the mode
    # below is read off the node's own description rather than assumed.
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
    return _reload(updated.name) if reload else updated


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
    """Bring one of a project's resources back to this machine.

    Removes the node binding recorded by `plexora.attach_table`,
    `plexora.attach_image` or `plexora.attach_segmentation`, and points the
    project at `path` instead. The project's own answers about the data --
    roles, the marker split, the coordinate source, whether values are
    log-transformed -- are untouched: a table that comes home is not a table
    that has to be re-imported.

    When to use:
        Call this when a node is being retired, or when a file that was
        being read remotely has been copied onto this machine, and the
        project should go back to reading it directly.

    Args:
        project (str | Project): The project to update, by name or object.
        kind (str): Which resource to detach: `"image"`, `"segmentation"` or
            `"table"`.
        path (str, optional): Where the file is on THIS machine. Required for
            `"image"` and `"table"` -- a project resource on a node has no
            local copy by construction. For `"segmentation"`, an empty path
            is a real answer: the project no longer has a mask.

    Returns:
        Project: The project, saved with the node binding removed and
        reloaded so this process serves it. Returned unchanged (and not
        reloaded) if `kind` had no node binding to remove.

    Raises:
        KeyError: If `kind` is not one of `"image"`, `"segmentation"` or
            `"table"`, or `project` names an unknown project.
        ValueError: If `path` is required for `kind` and not given, or (for
            `"image"`) if the file at `path` cannot be read as an image, or
            does not match the image the project was built on.

    Example:
        ```python
        import plexora

        plexora.detach("tonsil", "table", path="/data/cells.csv")
        ```
    """
    if kind not in RESOURCE_KINDS:
        raise KeyError(f"Unknown resource kind: {kind!r}")
    project = _project(project)
    binding = project.resource(kind)
    if binding is None:
        return project

    path = str(path).strip() if path else ""
    if not path and kind != "segmentation":
        # Refusing here, rather than removing the binding and leaving the
        # project pointed at `node://...` with nothing able to read it, names
        # the field to use instead -- the only actionable thing to say.
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
    picks the change up without a restart.

    Right whenever the process doing the attaching is also the process serving
    -- a web route, the CLI, a script -- which is every caller here except one.
    A notebook kernel attaches resources for a SIDECAR to serve, and running
    this there would read the table into the kernel a second time and open the
    image there, filling globals nothing in that process ever reads. That is
    what `reload=False` on the three `attach_*` functions is for; the sidecar is
    then asked to reload itself over HTTP (see `plexora/memory.py`).
    """
    from plexora.server.models import data_model

    data_model.load_datasource(name, reload=True)
    return Project.load(name)
