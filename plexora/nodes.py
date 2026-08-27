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
from dataclasses import replace
from pathlib import Path

from plexora.server.models import nodes as node_registry
from plexora.server.models.nodes import Node
from plexora.server.models.project import (
    RESOURCE_KINDS,
    DataSpec,
    Project,
    ResourceBinding,
)
from plexora.server.providers import http


def _now():
    return datetime.datetime.now().isoformat()


def register_node(name, endpoint, token=None, browser_endpoint=None, verify=True):
    """Record how to reach a data node, and check that it answers.

    `endpoint` is how THIS machine reaches it. `browser_endpoint` is how the
    user's browser does, when that is a different address -- an Open OnDemand
    portal path (`/rnode/compute-3/8642/`), or a tunnelled loopback port. Leave
    it unset whenever the two are the same, which is the desktop and Docker
    case and most tunnels.

    `verify=False` records the node without contacting it. For the case the
    check cannot cover: registering a node that is not up yet, from a script
    that starts it afterwards.
    """
    if not name or not str(name).strip():
        raise ValueError("a node needs a name -- it is what a project points at")
    node = Node(
        name=str(name).strip(),
        endpoint=str(endpoint).rstrip("/"),
        token=str(token or ""),
        browser_endpoint=(str(browser_endpoint).rstrip("/") if browser_endpoint else None),
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


def attach_table(project, node, resource_id, spec=None, table=None, **spec_fields):
    """Point a project's cell table at a node, and load it once.

    The project keeps every answer about what the table MEANS -- which column
    is the cell id, which matrix holds the intensities, whether the values are
    log-transformed. Those travel to the node with each load and are never
    stored there.

    `spec` (or the `spec_fields` keywords) describes how to read the file. If
    neither is given and the project already has a table spec, that one is
    reused with its `src` pointed at the node -- which is the ordinary way to
    move an existing project's table onto a node without re-answering anything.

    Returns the updated Project.
    """
    project = _project(project)
    entry = node_registry.get(str(node))

    read_spec = _read_spec_for(project, spec, table, spec_fields, entry, resource_id)
    described = http.json_request(
        entry, "POST", f"/node/v1/table/{resource_id}/load",
        body={"spec": read_spec.to_dict(), "reload": True},
        timeout=600.0, expected_api=node_registry.API_VERSION)

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

    The mask must already be a servable label pyramid on the node -- a node
    does not convert one on a primary's behalf, because conversion writes a
    derived file and where that file goes is a question about somebody's disk
    quota rather than about Plexora. `plexora node serve` prints what a mask
    needs; convert it there first if it is not ready.
    """
    project = _project(project)
    entry = node_registry.get(str(node))
    binding = ResourceBinding(
        kind="segmentation", provider="node", node=entry.name,
        resource_id=str(resource_id),
        capabilities=tuple(_capabilities(entry)),
    )
    segmentation = replace(project.segmentation, derived=f"node://{entry.name}/{resource_id}",
                           status="ready")
    updated = project.patch(segmentation=segmentation).with_resource(
        "segmentation", binding)
    updated.save()
    return _reload(updated.name)


def detach(project, kind):
    """Bring a resource back to being local, or to being absent.

    The counterpart of the three `attach_*` calls, and the reason the binding
    is a separate record from the path: removing it leaves the project's own
    answers -- roles, marker split, coordinate source -- exactly as they were,
    so a table that comes home is not a table that has to be re-imported.
    """
    if kind not in RESOURCE_KINDS:
        raise KeyError(f"Unknown resource kind: {kind!r}")
    project = _project(project)
    updated = project.with_resource(kind, None)
    updated.save()
    return _reload(updated.name)


# -- internals ------------------------------------------------------------


def _project(project) -> Project:
    return project if isinstance(project, Project) else Project.load(str(project))


def _capabilities(entry):
    """What the node says it can run, recorded so a control can be offered or
    not rather than offered and then failing."""
    try:
        return http.hello(entry, timeout=10.0).get("capabilities") or []
    except Exception:
        return []


def _read_spec_for(project, spec, table, spec_fields, entry, resource_id) -> DataSpec:
    if spec is not None:
        return spec if isinstance(spec, DataSpec) else DataSpec.from_dict(dict(spec))
    if spec_fields or table:
        fields = dict(spec_fields)
        if table:
            fields["table"] = table
        fields.setdefault("src", f"node://{entry.name}/{resource_id}")
        fields.setdefault("type", "anndata")
        built = DataSpec.from_dict(fields)
        if built is None:
            raise ValueError("the read spec needs at least a `type`")
        return built
    if project.dataset is not None:
        return project.dataset
    raise ValueError(
        f"{project.name!r} has no table spec yet, so there is nothing to tell "
        f"the node how to read the file. Pass spec=... (see "
        f"plexora.nodes.inspect_table) or import the table locally first.")


def _reload(name):
    """Re-read the project through the provider layer, so the running server
    picks the change up without a restart."""
    from plexora.server.models import data_model

    data_model.load_datasource(name, reload=True)
    return Project.load(name)
