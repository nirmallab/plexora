"""Serving a notebook kernel's own objects to the viewer, without a disk write.

    import plexora, scanpy as sc

    sc.tl.leiden(adata)
    plexora.view("tonsil", image="slide.ome.tif", adata=adata,
                 tool="cell_explorer", overlay="leiden")

The obstacle this removes is structural rather than an oversight. `plexora.view`
starts a SIDECAR -- a second OS process -- because the viewer's Flask app is
built at `import plexora` and cannot be rebuilt, because plugin blueprints are
chosen at import from the environment, and because one process serves one loaded
project through module globals that user code has no business sharing. A
separate process cannot read a Python object out of this one, which is exactly
why `register_anndata_datasource(adata=...)` has always written an `.h5ad` first.

**So the kernel becomes a data node.** Plexora already has a complete answer to
"the bytes are in another process": the node API, built for a compute node
holding a slide the viewer's machine cannot see. A node is a Flask app with the
viewer switched off (`server/node/app.py`) and a registry of resources
(`server/node/resources.py`), and the sidecar reads it through the ordinary
`Node*` providers off a `node://kernel/<id>` binding. Running one on a daemon
thread inside the kernel, over snapshots of the user's objects, needs no new
serving code on the viewer side at all -- the sidecar cannot tell this node from
one on a cluster, and neither can the browser.

Three consequences worth stating plainly:

**It is loopback, always.** The sidecar is spawned BY the kernel, so the two are
on the same machine whatever that machine is -- a laptop, a JupyterHub server,
an Open OnDemand compute node, a Colab VM. The kernel node binds 127.0.0.1 with
no browser endpoint and no allowed origins, so the browser's direct-route probe
(`/resource_routing`) fails by design everywhere and tiles are proxied through
the sidecar, which is the existing, tested fallback. No second port is ever
exposed, which is what makes the hosted environments work unchanged.

**Snapshots, not references.** A notebook goes on editing between cells. Reading
the live object would produce a table whose obs came from before an edit and
whose X came from after, at an unpredictable moment, with nothing on screen to
say so. `viewer.refresh()` takes a new snapshot and swaps it wholesale.

**A snapshot of a table costs memory.** obs, var and the one chosen matrix are
written into an in-memory zarr store, which is a copy -- roughly what the `.h5ad`
would have been, held in RAM instead of on disk. For the imaging tables this is
built for (10^5-10^6 cells, tens of markers) that is tens of megabytes. Images
and masks are NOT copied: level 0 is the caller's own array and only the coarse
levels are derived (see `server/utils/memory_pyramid.py`). A very large image
still belongs on disk, and `plexora.view(image="...")` takes a path exactly as
it always did.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
from pathlib import Path

#: What the kernel's node is called in `nodes.json`. One per data root, and
#: re-registered under the same name every kernel, so a restart heals rather
#: than accumulating an entry per session -- the same discipline `plexora
#: connect` uses for the node on a user's laptop.
NODE_NAME = "notebook-kernel"

#: Threads the kernel node's waitress pool runs. Small on purpose: this pool
#: serves tiles from inside the process that is also doing the analysis, and a
#: wide pool would compete with the user's own cells for cores it was never
#: asked for. Tiles are cached twice over on the way out (the sidecar's encoded
#: tile LRU and the node's own), so a pan after the first pass asks this node
#: for nothing at all.
NODE_THREADS = 4

#: An in-memory table larger than this gets one line saying so. Not a refusal:
#: the caller may well mean it, and the alternative -- writing the same bytes
#: to an .h5ad -- is not obviously cheaper. It is worth saying because the copy
#: is invisible otherwise.
LARGE_TABLE_BYTES = 2 * 1024 * 1024 * 1024

#: Same, for an image handed over as an array. The threshold is lower because
#: the answer is better: a path costs nothing at all here, and a whole-slide
#: image is the case the file-based workflow was built for.
LARGE_IMAGE_BYTES = 4 * 1024 * 1024 * 1024

#: obsm arrays wider than this are left out of a table snapshot unless named.
#: An obsm entry is (n_obs, k), and the ones that matter to a viewer -- spatial
#: coordinates, a UMAP -- have k of 2 or 3. The ones that do not are embeddings:
#: a 1536-dimensional one over 250k cells is 1.5 GB, and copying it so that it
#: can appear in a dropdown nobody is going to open is exactly the duplication
#: this module exists to avoid.
OBSM_WIDTH_LIMIT = 32


class MemoryDataError(RuntimeError):
    """Something about serving an in-memory object could not be done."""


# -- snapshots -------------------------------------------------------------


def snapshot_anndata(adata, *, layer=None, obsm_keys=None, include_x=True,
                     log=print):
    """A `TableSnapshot` over the elements a viewer actually reads.

    Written into a zarr `MemoryStore` with anndata's own `write_elem`, which is
    what makes `MemoryAnnDataAdapter` a one-method override: the group that
    comes out is byte-for-byte the group an `.h5ad` would have produced, so the
    adapter's subset, coordinate, identifier and blocked-matrix logic all run
    unchanged against it.

    Only what is needed. `obs` and `var` always; `X` or the ONE named layer,
    never both; and obsm arrays narrow enough to be coordinates rather than
    embeddings (see `OBSM_WIDTH_LIMIT`) plus any explicitly named in
    `obsm_keys`. What is left out is said out loud, because a coordinate source
    that is silently absent from the project's obsm list is a dropdown with a
    missing entry and no explanation.

    That is the one way a memory-served project differs from the same file
    imported: it offers no OTHER matrix to switch its features or coordinates
    to, because the other matrices were never copied. In a notebook the answer
    to "read the scaled layer instead" is to call `plexora.view` again saying
    so, which is a line above the one that opened it.
    """
    _require_anndata_writer()

    import zarr
    from anndata.io import write_elem

    from plexora.server.providers.memory import TableSnapshot

    store = zarr.storage.MemoryStore()
    root = zarr.open_group(store=store, mode="w")
    write_elem(root, "obs", adata.obs)
    write_elem(root, "var", adata.var)

    if layer:
        if layer not in adata.layers:
            raise MemoryDataError(
                # `sorted(adata.layers)` would raise: anndata's Layers mapping
                # yields None alongside the real names, standing for X.
                f"this AnnData has no layer named {layer!r} (it has: "
                f"{', '.join(sorted(k for k in adata.layers if k)) or 'none'})")
        write_elem(root, "layers", {layer: adata.layers[layer]})
    elif include_x and adata.X is not None:
        write_elem(root, "X", adata.X)

    wanted, skipped = _obsm_selection(adata, obsm_keys)
    if wanted:
        write_elem(root, "obsm", {key: adata.obsm[key] for key in wanted})
    if skipped:
        log(f"Plexora: not copying obsm {', '.join(sorted(skipped))} into the "
            f"viewer -- more than {OBSM_WIDTH_LIMIT} columns wide. Pass "
            f"obsm_keys=[...] to include one.")

    snapshot = TableSnapshot(group=zarr.open_group(store=store, mode="r"),
                             nbytes=_store_bytes(store))
    if snapshot.nbytes > LARGE_TABLE_BYTES:
        log(f"Plexora: this table is {_gb(snapshot.nbytes)} in memory. It is "
            f"held alongside your own object for as long as the viewer is "
            f"open; pass to_disk=True to write an .h5ad instead.")
    return snapshot


def snapshot_frame(frame):
    """A `TableSnapshot` over a flat table -- pandas, polars, or anything
    polars can take.

    Converted to polars once here rather than on every read, because a polars
    frame is what `NormalizedDatasource` holds and what every query the viewer
    runs is expressed over. A pandas frame of numeric columns converts through
    Arrow without copying the values.
    """
    import polars as pl

    from plexora.server.providers.memory import TableSnapshot

    if not isinstance(frame, pl.DataFrame):
        frame = pl.DataFrame(frame)
    return TableSnapshot(frame=frame, nbytes=int(frame.estimated_size()))


def snapshot_image(array, *, metadata=None, log=print):
    """An `ImageSnapshot` over a (channel, rows, cols) array.

    Level 0 is the caller's array, not a copy. The coarse levels are derived
    here -- about a third of level 0 again -- because a viewer asks for the
    whole field of view before it asks for any of it, and a single-level image
    would decode full resolution for every zoomed-out tile.

    The overview and the metadata are the other two thirds of what
    `LocalImageProvider.open()` returns, produced once at snapshot time for the
    same reason it produces them once per open: they come out of the same pass.
    """
    from plexora.server.utils import memory_pyramid, ome_zarr

    pyramid = memory_pyramid.image_pyramid(array)
    nbytes = memory_pyramid.level_bytes(pyramid)
    if nbytes > LARGE_IMAGE_BYTES:
        log(f"Plexora: this image needs {_gb(nbytes)} of pyramid in memory. An "
            f"image this size is usually better passed as a path -- the viewer "
            f"reads it from disk a tile at a time and copies nothing.")

    from plexora.server.providers.memory import ImageSnapshot

    return ImageSnapshot(
        pyramid=pyramid,
        overview=ome_zarr.overview_plane(pyramid),
        # Physical pixel size, if the caller supplied any. Synthesized rather
        # than read: an array carries no OME header, so the scale bar is off
        # unless somebody says what a pixel is worth.
        metadata=dict(metadata or {}),
        nbytes=nbytes,
    )


def snapshot_mask(array):
    """A `SegmentationSnapshot` over a 2-D label array.

    Costs no memory beyond the caller's own array: every coarse level is a
    strided view of level 0 (see `memory_pyramid.label_pyramid`), which is both
    the cheapest and the only correct way to downsample label ids.
    """
    from plexora.server.providers.memory import SegmentationSnapshot
    from plexora.server.utils import memory_pyramid, segmentation_pyramid

    pyramid = memory_pyramid.label_pyramid(array)
    return SegmentationSnapshot(
        pyramid=pyramid,
        mode=segmentation_pyramid.MODE_FILLED,
        nbytes=memory_pyramid.level_bytes(pyramid),
    )


def _obsm_selection(adata, obsm_keys):
    """(what to copy, what was left out) from `adata.obsm`. See OBSM_WIDTH_LIMIT."""
    named = {str(key) for key in (obsm_keys or ())}
    missing = named - set(adata.obsm)
    if missing:
        raise MemoryDataError(
            f"this AnnData has no obsm {', '.join(sorted(missing))} (it has: "
            f"{', '.join(sorted(adata.obsm)) or 'none'})")
    wanted, skipped = [], []
    for key in adata.obsm:
        value = adata.obsm[key]
        width = int(value.shape[1]) if getattr(value, "ndim", 0) > 1 else 1
        (wanted if key in named or width <= OBSM_WIDTH_LIMIT else skipped).append(key)
    return wanted, skipped


def _store_bytes(store) -> int:
    """How much a zarr MemoryStore is holding.

    Walked rather than asked, because a `MemoryStore` is a dict of buffers and
    reports no total. It is only ever called once per snapshot.
    """
    total = 0
    try:
        for value in store._store_dict.values():
            total += len(getattr(value, "to_bytes", lambda: value)())
    except Exception:
        return 0
    return total


def _gb(nbytes) -> str:
    return f"{nbytes / (1024 ** 3):.1f} GB"


def _require_anndata_writer():
    """Refuse early, and say which half is too old.

    `write_elem` against a zarr v3 store is what the whole AnnData path here
    rests on. Failing inside it produces an exception about encodings that says
    nothing about what to install.
    """
    try:
        import anndata  # noqa: F401
        from anndata.io import write_elem  # noqa: F401
    except ImportError as exc:
        raise MemoryDataError(
            "serving an AnnData from memory needs anndata with its public "
            "element writer (anndata >= 0.10). Install a newer anndata, or "
            "pass to_disk=True to write an .h5ad instead."
        ) from exc


# -- the node that lives in this kernel ------------------------------------


class KernelNode:
    """A Plexora data node on a daemon thread inside this interpreter.

    One per process, started the first time something is served and left
    running until the interpreter ends. It holds no files, writes nothing, and
    binds loopback with a token -- so on a shared machine it is no more
    reachable than any other loopback port, and less, because it answers 403
    without the token.

    Nothing polls it and it polls nothing. The sidecar reaches it exactly as it
    would reach a node on a cluster, and if this kernel dies the sidecar
    degrades through the existing unavailable-resource path -- the project stays
    open, the layers that were on this node report that they cannot be reached,
    and the next `plexora.view()` from a fresh kernel re-registers everything
    under the same deterministic names.
    """

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls, log=print) -> "KernelNode":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(log=log)
            else:
                cls._instance._ensure_registered(log=log)
            return cls._instance

    def __init__(self, log=print):
        from plexora.server.node import resources as node_resources
        from plexora.server.node.app import create_node_app

        # Before the node answers anything, and the one thing in this
        # constructor that is not obviously necessary. Importing a C extension
        # holds the GIL across dlopen while threadpoolctl's library walk wants
        # the loader lock and then the GIL back; a first tile encode racing a
        # first mixture fit has frozen a whole interpreter that way. Paid here,
        # on one thread, exactly as `serve_node` pays it before its announce.
        from plexora.server.models import data_model

        data_model.prime_hot_code()

        self.registry = node_resources.Registry()
        self.token = secrets.token_hex(16)
        self.node_id = f"kernel-{os.getpid()}-{secrets.token_hex(3)}"
        app = create_node_app(
            [], self.token, node_id=self.node_id,
            # No browser ever talks to this node -- see the module docstring --
            # so there is no origin to allow, and `dynamic` (which hands the
            # token holder arbitrary file reads) has nothing to offer a node
            # whose resources are objects.
            allow_origins=(), dynamic=False, registry=self.registry,
        )
        self._server = _serve_in_thread(app, threads=NODE_THREADS)
        self.port = int(self._server.effective_port)
        self._register(log=log)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _register(self, log=print):
        """Put this node in `nodes.json`, replacing whatever was there.

        Under a fixed name rather than a generated one, so a kernel restart
        overwrites the previous kernel's entry instead of leaving a dead node
        behind for every session. `verify=True` costs one loopback request and
        proves the thread is actually answering before anything is attached to
        it -- the alternative being an attach that fails with a connection
        error the user has no way to place.

        No `browser_endpoint`, and `role="kernel"`, which is what keeps this
        node out of `/resource_routing` altogether: its loopback address means
        the user's own laptop from where a hosted browser stands, and offering
        it would carry this node's token there. See `Node.browser_reachable`.
        """
        from plexora import nodes as node_api
        from plexora.server.models import nodes as node_registry

        node_api.register_node(NODE_NAME, self.endpoint, token=self.token,
                               role=node_registry.KERNEL, verify=True)

    def _ensure_registered(self, log=print):
        """Register again if the CURRENT data root does not know this node.

        `nodes.json` lives under a data root, and a notebook may perfectly well
        open one viewer under the default root and another under an explicit
        `data_dir=`. The node is one per interpreter and has to be findable
        from both, so this runs before every use rather than only at startup.
        Cheap: a JSON read, and nothing at all in the ordinary case.
        """
        from plexora.server.models import nodes as node_registry

        entry = node_registry.find(NODE_NAME)
        if entry is not None and entry.endpoint == self.endpoint \
                and entry.token == self.token:
            return
        self._register(log=log)

    def serve(self, kind, resource_id, snapshot):
        """Start serving one snapshot, or replace the one under that id."""
        return self.registry.add_memory(kind, resource_id, snapshot)

    def serving(self, resource_id) -> bool:
        return any(r.id == resource_id for r in self.registry.all())

    def drop(self, resource_id) -> bool:
        return self.registry.remove(resource_id)


def _serve_in_thread(app, threads=NODE_THREADS):
    """Run `app` on a daemon thread and hand back the server.

    `create_server` rather than `serve`, because the port has to be readable
    before the accept loop starts: the node binds port 0 (let the OS choose --
    a kernel must not fight another kernel for a fixed one) and the caller
    needs the answer to register it.

    A daemon thread because the kernel's lifetime is the user's, not this
    server's: a notebook that has finished with the viewer should exit, not
    hang on an accept loop nobody is talking to.
    """
    from waitress import create_server

    server = create_server(app, host="127.0.0.1", port=0, threads=threads)
    thread = threading.Thread(target=server.run, name="plexora-kernel-node",
                              daemon=True)
    thread.start()
    return server


# -- registering a project over in-memory objects --------------------------


def register_memory_datasource(
    name,
    image=None,
    *,
    segmentation=None,
    adata=None,
    table=None,
    sdata=None,
    sdata_table=None,
    sdata_image=None,
    sdata_labels=None,
    channel_names=None,
    coordinate_source=None,
    obsm_key=None,
    x=None,
    y=None,
    feature_source="X",
    layer=None,
    feature_obs_columns=None,
    obs_id_field=None,
    celltype_column=None,
    subset_by=None,
    subset_value=None,
    apply_log_transform=False,
    pixel_size=None,
    data_dir=None,
    image_type=None,
    segmentation_mode=None,
    log=print,
):
    """Register a project whose resources live in this kernel's memory.

    Each of `image`, `segmentation` and the table is independently a path or an
    object. A path takes the ordinary local route -- the same conversion, the
    same pyramid, the same performance the viewer has always had -- and an
    object is snapshotted and served by this kernel's node. The mixed case is
    the one this is really for: a whole-slide image on disk with an AnnData
    that a notebook has just finished annotating.

    Calling this twice with the same name REPLACES the snapshots and re-reads
    the table's shape, while leaving everything else about the project alone --
    its saved channels, its ROIs, its figures, its plugin state, all of which
    live outside config.json. That is what makes the analysis loop cheap:
    annotate, call again, look. It is also why the re-read matters: an obs
    column that did not exist last cell has to reach the project's recorded
    vocabulary, or nothing offers it as an overlay.

    Anything passed as None is left exactly as it is, which is what makes the
    second call cheap. A refresh names the table and nothing else, so a
    whole-slide image is neither re-read nor re-pyramided and the browser's
    tiles of it stay valid.

    A name already taken by a project this kernel does not own is refused
    rather than overwritten.

    Returns the Project. The caller is responsible for telling whichever server
    is serving it to reload (see `plexora.jupyter.PlexoraViewer.refresh`); this
    deliberately does not, because in notebook mode the serving process is not
    this one.
    """
    from plexora import paths
    from plexora.server.models.project import Project

    if adata is not None and table is not None:
        raise MemoryDataError("pass adata= or table=, not both -- they are two "
                              "ways of naming the same thing")
    if sdata is not None:
        image, segmentation, adata = _decompose_spatialdata(
            sdata, table=sdata_table, image=sdata_image, labels=sdata_labels,
            image_given=image, labels_given=segmentation, table_given=adata)

    data_root = Path(data_dir).expanduser().resolve() if data_dir else paths.data_root()
    (data_root / str(name)).mkdir(parents=True, exist_ok=True)
    kernel = KernelNode.get(log=log)

    ids = _resource_ids(name)
    existing = Project.find(name, data_root)
    _refuse_foreign(existing, ids)

    project = _ensure_image(name, image, existing, ids, kernel,
                            channel_names=channel_names, image_type=image_type,
                            pixel_size=pixel_size, data_root=data_root, log=log)
    project = _ensure_segmentation(project, segmentation, ids, kernel,
                                   data_root=data_root,
                                   segmentation_mode=segmentation_mode)
    project = _ensure_table(
        project, adata=adata, table=table, ids=ids, kernel=kernel,
        coordinate_source=coordinate_source, obsm_key=obsm_key, x=x, y=y,
        feature_source=feature_source, layer=layer,
        feature_obs_columns=feature_obs_columns, obs_id_field=obs_id_field,
        celltype_column=celltype_column, subset_by=subset_by,
        subset_value=subset_value, apply_log_transform=apply_log_transform,
        log=log,
    )
    # Reloaded from disk rather than returned from the last patch, so what
    # comes back is exactly what the sidecar is about to read.
    return Project.load(name)


def _decompose_spatialdata(sdata, *, table, image, labels,
                           image_given, labels_given, table_given):
    """(image, labels, table) taken out of an in-memory SpatialData object.

    A SpatialData store is three of Plexora's resources in one container, and
    the viewer wants them separately -- so this is a decomposition, not a new
    reader. Once the pieces are out, each takes exactly the path it would have
    taken on its own: the table is an AnnData and is snapshotted as one, the
    image and the labels are arrays and are pyramided as such.

    An element named explicitly wins; otherwise a container holding exactly one
    of a kind is unambiguous and that one is taken. More than one and nobody
    named which is a question rather than a guess -- picking alphabetically
    would open the wrong slide without saying so.

    Anything the caller passed directly (`image=`, `segmentation=`, `adata=`)
    beats the store, which is what makes the common mixed case sayable: a
    SpatialData in memory for the table, and the real slide still on disk.

    **Coordinates are used as-is, in pixel space.** No coordinate-system
    transform is applied. That is the same contract `SpatialDataAdapter`
    documents for a store on disk, and the two must not disagree.
    """
    picked_table = table_given if table_given is not None else _one_of(
        getattr(sdata, "tables", {}), table, "table")
    picked_image = image_given if image_given is not None else _spatial_array(
        _one_of(getattr(sdata, "images", {}), image, "image", optional=True))
    picked_labels = labels_given if labels_given is not None else _spatial_array(
        _one_of(getattr(sdata, "labels", {}), labels, "labels", optional=True))
    return picked_image, picked_labels, picked_table


def _one_of(container, name, kind, optional=False):
    """One element of a SpatialData container -- the named one, or the only one."""
    keys = sorted(container)
    if name is not None:
        if str(name) not in container:
            raise MemoryDataError(
                f"this SpatialData has no {kind} called {str(name)!r} "
                f"(it has: {', '.join(keys) or 'none'})")
        return container[str(name)]
    if not keys:
        if optional:
            return None
        raise MemoryDataError(f"this SpatialData has no {kind} to read")
    if len(keys) > 1:
        raise MemoryDataError(
            f"this SpatialData has {len(keys)} {kind} elements "
            f"({', '.join(keys)}); say which one with sdata_{kind}=")
    return container[keys[0]]


def _spatial_array(element):
    """The pixels of a SpatialData image or labels element, as an array.

    An element is an xarray `DataArray`, or a `DataTree` when the store already
    holds several scales. The FINEST scale is taken in both cases and the
    coarser levels are re-derived here rather than adopted, because the viewer's
    tile source computes a level's size as `size >> level` -- a store whose
    scales are not a halving chain draws the wrong rectangle at the wrong zoom,
    with no error anywhere. `ome_zarr.dyadic_prefix` exists because real stores
    do this; re-deriving costs about a third of level 0 and cannot be wrong.

    `.data` unwraps the xarray, so a dask-backed element stays lazy for level 0.
    """
    if element is None:
        return None
    # A DataTree: children scale0, scale1, ..., each a Dataset holding one
    # variable. "scale0" sorts first, which is the finest.
    children = getattr(element, "children", None)
    if children:
        dataset = getattr(children[sorted(children)[0]], "ds", None)
        variables = getattr(dataset, "data_vars", None)
        if variables:
            element = next(iter(variables.values()))
    return getattr(element, "data", element)


def _resource_ids(name) -> dict:
    """This project's resource ids on the kernel node.

    Derived from the project name and stable across kernels, for the same
    reason `nodes.resource_id_for` derives an id from a path: the binding
    written into config.json this session has to find the same resource when
    the notebook is re-run tomorrow. Slugged because an id is a URL path
    segment, and project names have spaces in them.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(name)).strip("-").lower() or "project"
    return {kind: f"{slug}-{kind}" for kind in ("image", "segmentation", "table")}


def _refuse_foreign(project, ids):
    """Never overwrite a project this kernel did not make.

    Re-registering our own is the whole point -- that is `refresh`. Silently
    repointing somebody's existing project at a notebook's arrays is a different
    thing entirely, and the failure would be discovered by the user opening it
    and finding their slide replaced.
    """
    if project is None:
        return
    for kind, resource_id in ids.items():
        binding = project.resource(kind)
        if binding is None or not binding.is_node:
            continue
        if binding.node != NODE_NAME or binding.resource_id != resource_id:
            raise MemoryDataError(
                f"a project called {project.name!r} already exists and its "
                f"{kind} points somewhere else. Choose another name.")


def _is_path(value) -> bool:
    return isinstance(value, (str, os.PathLike))


def _ensure_image(name, image, existing, ids, kernel, *, channel_names,
                  image_type, pixel_size, data_root, log):
    """The project, with its image in place. Creates it when it is not there.

    A path goes through `register_image_datasource`, which is the ordinary
    import: the file is read, its pyramid is described, and the project records
    a local path. An array is snapshotted onto the kernel node and attached,
    which is `attach_image` doing exactly what it does for a cluster node.

    An image the caller did not name again is not touched, and neither is one
    already registered from the same file. Both are what keep a refresh cheap:
    reading a whole-slide image's geometry is seconds, and a project whose
    image did not move must not have its tile ETags invalidated for nothing.
    """
    from plexora import datasource as datasource_api
    from plexora import nodes as node_api
    from plexora.server.models.project import ImageSpec, Project

    if image is None:
        if existing is None:
            raise MemoryDataError(
                f"there is no project called {name!r} yet, so this call has to "
                f"say which image it is of")
        return existing

    if _is_path(image):
        resolved = datasource_api._resolve_image(Path(image).expanduser().resolve())
        if (existing is not None and existing.image.width
                and str(existing.image.src) == str(resolved)):
            return existing
        datasource_api.register_image_datasource(
            name, image, channel_names=channel_names, data_dir=data_root,
            image_type=image_type)
        return Project.load(name)

    snapshot = snapshot_image(image, metadata=_pixel_metadata(pixel_size), log=log)
    kernel.serve("image", ids["image"], snapshot)
    if existing is None:
        Project(name=str(name), image=ImageSpec()).save(data_root)
    # reload=False: the process attaching is the notebook kernel, and the
    # process serving is the sidecar. See nodes._reload.
    return node_api.attach_image(str(name), node=NODE_NAME,
                                 resource_id=ids["image"],
                                 channel_names=channel_names, reload=False)


def _pixel_metadata(pixel_size):
    """OME-ish physical size for an array that carries no header.

    `physical_metadata` produces the same two keys from an NGFF store's axis
    scales, and the scale bar reads them off the project. A number means
    micrometres, which is what every microscope in this field reports.
    """
    if pixel_size is None:
        return {}
    return {"physical_size_x": float(pixel_size), "physical_size_x_unit": "µm",
            "physical_size_y": float(pixel_size), "physical_size_y_unit": "µm"}


def _ensure_segmentation(project, segmentation, ids, kernel, *, data_root,
                         segmentation_mode):
    """`project` with its mask in place, or unchanged when there is none."""
    from dataclasses import replace

    from plexora import datasource as datasource_api
    from plexora import nodes as node_api

    if segmentation is None:
        return project

    if _is_path(segmentation):
        fields, pending = datasource_api._segmentation_config_fields(
            segmentation, data_root / project.name, False, segmentation_mode)
        updated = project.patch(
            image=replace(project.image, channels=tuple(
                datasource_api._with_area_channel(
                    project.name, project.image.channels, fields["segmentation"]))),
            segmentation=datasource_api._segmentation_spec(fields),
        )
        updated.save()
        return updated

    kernel.serve("segmentation", ids["segmentation"], snapshot_mask(segmentation))
    return node_api.attach_segmentation(project.name, node=NODE_NAME,
                                        resource_id=ids["segmentation"],
                                        reload=False)


def _ensure_table(project, *, adata, table, ids, kernel, coordinate_source,
                  obsm_key, x, y, feature_source, layer, feature_obs_columns,
                  obs_id_field, celltype_column, subset_by, subset_value,
                  apply_log_transform, log):
    """`project` with its cell table in place, or unchanged when there is none.

    The spec is built by `datasource.anndata_spec`/`flat_table_spec` -- the
    same translation the import form and `register_anndata_datasource` use --
    and then described by a `plan()` against the snapshot, exactly as a file
    import describes it by a `plan()` against the file. That is what makes a
    project registered here indistinguishable from one imported the long way:
    same marker/metadata split, same obs/layer/obsm vocabularies, same roles.
    """
    from plexora import datasource as datasource_api
    from plexora import nodes as node_api
    from plexora.server.providers.memory import locator_for

    if adata is None and table is None:
        return project

    resource_id = ids["table"]
    src = locator_for(resource_id)

    if adata is not None:
        snapshot = snapshot_anndata(
            adata, layer=layer if feature_source == "layer" else None,
            obsm_keys=[obsm_key] if obsm_key else None, log=log)
        spec = datasource_api.anndata_spec(
            src, coordinate_source=coordinate_source, obsm_key=obsm_key,
            x=x, y=y, feature_source=feature_source, layer=layer,
            feature_obs_columns=feature_obs_columns, subset_by=subset_by,
            subset_value=subset_value, apply_log_transform=apply_log_transform,
            obs_id_field=obs_id_field, celltype_column=celltype_column)
        spec = datasource_api.described_spec(spec, snapshot.adapter(spec).plan())
    else:
        snapshot = snapshot_frame(table)
        spec = datasource_api.flat_table_spec(
            src,
            [{"name": column, "dtype": str(dtype)}
             for column, dtype in snapshot.frame.schema.items()],
            x=x, y=y, id_column=obs_id_field, celltype_column=celltype_column)

    kernel.serve("table", resource_id, snapshot)
    return node_api.attach_table(project.name, node=NODE_NAME,
                                 resource_id=resource_id, spec=spec,
                                 reload=False)


def serves_memory(project) -> bool:
    """Whether any of this project's resources live in a kernel's memory.

    Read off the bindings rather than remembered, so it is true of a project
    registered by an earlier cell, an earlier kernel, or a script -- which is
    what `viewer.refresh()` needs to know before it offers to re-snapshot
    anything.
    """
    return any((binding := project.resource(kind)) is not None
               and binding.is_node and binding.node == NODE_NAME
               for kind in ("image", "segmentation", "table"))
